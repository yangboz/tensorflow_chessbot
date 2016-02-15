#!/usr/bin/python
# -*- coding: utf-8 -*-
# Finds submissions with chessboard images in them,
# use a tensorflow convolutional neural network to predict pieces and return
# a lichess analysis link and FEN diagram of chessboard
import praw
import collections
import os
import time
from datetime import datetime
from praw.helpers import submission_stream
import requests
import socket

import auth_config # for PRAW
import tensorflow_chessbot # For neural network model

#########################################################
# Setup

# Set up praw
chess_fen_bot = "ChessFenBot"
user_agent = chess_fen_bot + " finds chessboard image posts, uses convolutional neural network to responds with FEN diagram + analysis link. See https://github.com/Elucidation/tensorflow_chessbot"

# Login
r = praw.Reddit(user_agent=user_agent)

# Login old-style due to Reddit politics
r.login(auth_config.USERNAME, auth_config.PASSWORD, disable_warning=True)

# Get accessor to subreddit
subreddit = r.get_subreddit('chess')

# How many submissions to read from initially
submission_read_limit = 300

# Filename containing list of submission ids that 
# have already been processed, updated at end of program
processed_filename = "submissions_already_processed.txt"

# Submissions computer vision or prediction failed on
failures_filename = "submission_failures.txt"

responses_filename = "submission_responses.txt"

#########################################################
# PRAW Helper Functions

def isChessboardTopic(sub):
  """If white/black (to play), and url is imgur link"""
  return any([q in sub.title.lower() for q in ['white', 'black']]) \
         and sub.url != None and 'imgur' in sub.url

def getResponseToChessboardTopic(title, fen, certainty):
  """Parse white/black to play from title, and use prediction results for output"""
  # Default white to play
  to_play = '_w'
  to_play_full = 'White'
  lichess_analysis = 'http://www.lichess.org/analysis/%s%s' % (fen, to_play)
  fen_img_link = 'http://www.fen-to-image.com/image/30/%s.png' % fen
  black_addendum = ""

  if isBlackToPlay(title):
    to_play = '_b'
    to_play_full = 'Black'

    # Flip fen order for black to play, assumes screenshot is flipped
    # fen = '/'.join((reversed(fen.split('/'))))
    original_fen = fen
    fen = ''.join(reversed(fen))
    fen_img_link = 'http://www.fen-to-image.com/image/30/%s.png' % fen
    lichess_analysis = 'http://www.lichess.org/analysis/%s%s' % (fen, to_play)
    
    # Unflipped
    original_lichess_analysis = 'http://www.lichess.org/analysis/%s%s' % (original_fen, to_play)
    original_fen_img_link = 'http://www.fen-to-image.com/image/30/%s.png' % original_fen
    black_addendum = ("\n\n*If board is transposed:*"
                      "\n\nReversed Fen: [%s](%s)"
                      "\n\nReversed [Lichess analysis link](%s)" % (original_fen, original_fen_img_link, original_lichess_analysis))


  msg = ("I attempted to generate a chessboard layout from the posted image, with an overall certainty of **%g%%**.\n\n"
         "FEN: [%s](%s)\n\n"
         "Here is a link to a [Lichess Analysis](%s) - %s to play%s"
         % (round(certainty*100, 4), fen, fen_img_link, lichess_analysis, to_play_full, black_addendum))
  return msg

def isBlackToPlay(title):
  """Based on post title return if it's black to play (default is white)"""
  return 'black to play' in title.lower() or ('black' in title.lower() and 'white' not in title.lower())

def getResponseHeader():
  return "ChessFenBot [◕ _ ◕]^*  ^(*I make FENs*)\n\n---\n\n"

def getResponseFooter(title, fen):
  to_play = '_w'
  black_addendum = ""
  if isBlackToPlay(title):
    to_play = '_b'
    original_fen = fen
    fen = ''.join(reversed(fen))
    original_lichess_editor = 'http://www.lichess.org/editor/%s%s' % (original_fen, to_play)
    black_addendum = "^(/)[^((Flipped))](%s)" % original_lichess_editor
  
  lichess_editor = 'http://www.lichess.org/editor/%s%s' % (fen, to_play)

  return ("\n\n---\n\n"
         "^(Yes I am a machine learning bot | )"
         "[^(`How I work`)](https://github.com/Elucidation/tensorflow_chessbot 'Must go deeper')"
         "^( | Reply with a corrected FEN or )[^(Editor link)](%s)%s^( to add to my next training dataset)" % (lichess_editor, black_addendum))

def waitWithComments(sleep_time, segment=60):
  """Sleep for sleep_time seconds, printing to stdout every segment of time"""
  print("\t%s - %s seconds to go..." % (datetime.now(), sleep_time))
  while sleep_time > segment:
    time.sleep(segment) # sleep in increments of 1 minute
    sleep_time -= segment
    print("\t%s - %s seconds to go..." % (datetime.now(), sleep_time))
  time.sleep(sleep_time)

def logInfoPerSubmission(submission, count, count_actual):
  if ((time.time() - logInfoPerSubmission.last) > 120):
    print("\n\t---\n\t%s - %d processed submissions, %d read\n" % (datetime.now(), count_actual, count))
    logInfoPerSubmission.last = time.time()
  try:
    print("#%d Submission(%s): %s" % (count, submission.id, submission))
  except UnicodeDecodeError as e:
    print("#%d Submission(%s): <ignoring unicode>" % (count, submission.id))


logInfoPerSubmission.last = time.time() # 'static' variable

def loadProcessed(processed_filename=processed_filename):
  if not os.path.isfile(processed_filename):
    print("%s - Starting new processed file" % datetime.now())
    return set()
  else:
    print("Loading existing processed file...")
    with open(processed_filename,'r') as f:
      return set([x.strip() for x in f.readlines()])

def saveProcessed(already_processed, processed_filename=processed_filename):
  with open(processed_filename,'w') as f:
    for submission_id in already_processed:
      f.write("%s\n" % submission_id)
  print("%s - Saved processed ids to file" % datetime.now())

def addSubmissionToFailures(submission, failures_filename=failures_filename):
  with open(failures_filename,'a') as f:
    f.write("%s : %s | %s\n" % (submission.id, submission.title, submission.url))
  print("%s - Saved failure to file" % datetime.now())  

def addSubmissionToResponses(submission, fen, certainty, responses_filename=responses_filename):
  # Reverse fen if it's black to play, assuming board is flipped
  # This is causes issues, need to find a way to determine if board is flipped
  # when black to play, because sometimes people don't screenshot with board
  # flipped from black viewpoint, and other times they do.
  
  if isBlackToPlay(submission.title):
    fen = ''.join(reversed(fen))
  with open(responses_filename,'a') as f:
    f.write("%s : %s | %s | %s %g\n" % (submission.id, submission.title, submission.url, fen, certainty))
  print("%s - Saved response to file" % datetime.now())  

#########################################################
# Main Script
# Track commend ids that have already been processed successfully

# Load list of already processed comment ids
already_processed = loadProcessed()
print("%s - Starting with already processed: %s\n==========\n\n" % (datetime.now(), already_processed))

count = 0
count_actual = 0
running = True

# Start up Tensorflow CNN with trained model
predictor = tensorflow_chessbot.ChessboardPredictor()

while running:
  # get submission stream
  try:
    submissions = submission_stream(r, subreddit, limit=submission_read_limit)
    # for each submission
    for submission in submissions:
      count += 1
      # print out some debug info
      logInfoPerSubmission(submission, count, count_actual)

      # Skip if already processed
      if submission.id in already_processed:
        continue
      
      # check if submission title is a question
      if isChessboardTopic(submission):
        
        # Use CNN to make a prediction
        print "Image URL: %s" % submission.url
        fen, certainty = predictor.makePrediction(submission.url)
        print "Predicted FEN: %s" % fen
        print "Certainty: %.1f%%" % (certainty*100)

        if fen is None:
          print("> %s - Couldn't generate FEN, skipping..." % datetime.now())
          addSubmissionToFailures(submission)
          continue

        # generate response
        msg = "%s%s%s" % (
          getResponseHeader(),
          getResponseToChessboardTopic(submission.title, fen, certainty), \
          getResponseFooter(submission.title, fen))
        # respond, keep trying till success
        while True:
          try:
            print("> %s - Responding to %s: %s" % (datetime.now(), submission.id, submission))
            print "\tURL:", submission.url
            
            # Reply with comment
            submission.add_comment(msg)
            
            # update & save list
            already_processed.add(submission.id)
            saveProcessed(already_processed)
            addSubmissionToResponses(submission, fen, certainty)

            count_actual += 1
            # Wait after submitting to not overload
            waitWithComments(600)
            break
          except praw.errors.AlreadySubmitted as e:
            print("> %s - Already submitted skipping..." % datetime.now())
            break
          except praw.errors.RateLimitExceeded as e:
            print("> {} - Rate Limit Error for commenting on {}, sleeping for {} before retrying...".format(datetime.now(), submission.id, e.sleep_time))
            waitWithComments(e.sleep_time)

  
  # Handle errors
  except (socket.error, requests.exceptions.ReadTimeout, requests.packages.urllib3.exceptions.ReadTimeoutError, requests.exceptions.ConnectionError) as e:
    print("> %s - Connection error, resetting accessor, waiting 30 and trying again: %s" % (datetime.now(), e))
    saveProcessed(already_processed)
    time.sleep(30)
    continue
  except Exception as e:
    print("Unknown Error, continuing after 30:",e)
    time.sleep(30)
    continue
  except KeyboardInterrupt:
    print("Exiting...")
    running = False
  finally:
    saveProcessed(already_processed)
    print("%s - All Processed:\n%s" % (datetime.now(),already_processed))


print("%s - Program Ended. Total Processed Submissions (%d replied / %d read):\n%s" % (datetime.now(), count_actual, count, already_processed))
