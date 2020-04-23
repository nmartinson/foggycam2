#!/usr/bin/env python3

"""FoggyCam captures Nest camera images and generates a video."""

import os
from collections import defaultdict
import traceback
from subprocess import Popen, PIPE, call
from shlex import split as shsplit
import uuid
import threading
import time
from datetime import datetime
import shutil
import requests
from re import search as re_search
import emailsender

# TODO: refactor jpg clean off (CPU intensive) with bash shell 'rm -f'
# TODO: move image compression into a separate thread. Recording is paused while compressing and uploading video
# TODO: option exclude cameras
# TODO: bundle the folders creation
# TODO: refactor to use module tempfile for directories and file names
# TODO: retention period for files and videos
# TODO: add logs and restructure printing

# intended to be used for compiling video on exception
camera_name = ''
camera_path = ''
video_path = ''
camera = {}
camera_buffer = []
file_id = 0
image_path = ''

class FoggyCam(object):
  """FoggyCam client class that performs capture operations."""

  nest_user_id = ''
  nest_access_token = ''
  # nest_access_token_expiration = ''

  nest_user_url = 'https://home.nest.com/api/0.1/user/#USERID#/app_launch'
  nest_image_url = 'https://nexusapi-#REGION#.camera.home.nest.com/get_image?uuid=#CAMERAID#&width=#WIDTH#&cachebuster=#CBUSTER#'
  nest_auth_url = 'https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt'
  user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/75.0.3770.100 Safari/537.36'

  nest_user_request_payload = {
    "known_bucket_types": ["quartz"],
    "known_bucket_versions": []
  }

  nest_camera_array = []
  nest_camera_buffer_threshold = 50

  is_capturing = False
  cam_retry_wait = 60
  temp_dir_path = ''
  local_path = ''

  def __init__(self, config):
    self.config = config
    self.nest_access_token = None

    if not os.path.exists('_temp'):
      os.makedirs('_temp')

    self.cam_retry_wait = config.cam_retry_wait or self.cam_retry_wait
    self.local_path = os.path.dirname(os.path.abspath(__file__))
    self.temp_dir_path = os.path.join(self.local_path, '_temp')
    self.nest_camera_buffer_threshold = self.config.threshold or self.nest_camera_buffer_threshold

    self.ffmpeg_path = False

    self.time_stamp = self.config.time_stamp or False
    self.convert_path = False

    self.check_tools()
    self.get_authorization()
    self.initialize_user()
    self.capture_images()

  def check_tools(self):
    imagemagic = shutil.which("convert")

    if self.config.produce_video:
      ffmpeg_tool_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tools', 'ffmpeg'))
      ffmpeg_local_path = shutil.which("ffmpeg")

      if ffmpeg_local_path:
        self.ffmpeg_path = ffmpeg_local_path
      elif os.path.isfile(ffmpeg_tool_path):
        self.ffmpeg_path = ffmpeg_tool_path
      else:
        print("<> WARNING: could not find 'ffmpeg' skipping image to video compression. Try installing it:\n")

    if self.time_stamp and imagemagic:
      self.convert_path = imagemagic
    else:
      print("<> WARNING: could not find '/usr/bin/convert' skip applying time stamp. Try installing it:\n")

  @staticmethod
  def run_requests(url, method, headers=None, params=None, payload=None):
    method = method.lower()
    try:
      with requests.Session() as s:
        if method == 'get':
          r = s.get(url=url, headers=headers)
        elif method == 'post':
          r = s.post(url=url, headers=headers, params=params, json=payload)
        else:
          class X: reason = "Failed: un-managed method: {}".format(method)
          return False, X
        return True, r
    except Exception as all_error:
      print("<> ERROR: failed to perform request using: \n"
            "<> URL: {}\n"
            "<> HEADERS: {}\n"
            "<> PARAMS: {}\n"
            "<> RECEIVED ERROR: \n{}".format(url, headers, params, all_error))
      return False, all_error

  @staticmethod
  def now_time(form='%Y-%m-%d %H:%M:%S'):
    return datetime.now().strftime(form)

  @staticmethod
  def createAndGetOutputPaths(self, camera_name) :
    # Determine whether the entries should be copied to a custom path
    # or not.
    if not self.config.path:
      camera_path = os.path.join(self.local_path, 'capture', camera_name, 'images')
      video_path = os.path.join(self.local_path, 'capture', camera_name, 'video')
    else:
      camera_path = os.path.join(self.config.path, 'capture', camera_name, 'images')
      video_path = os.path.join(self.config.path, 'capture', camera_name, 'video')

    # Provision the necessary folders for images and videos.
    if not os.path.exists(camera_path):
      os.makedirs(camera_path)

    if not os.path.exists(video_path):
      os.makedirs(video_path)

    return camera_path, video_path

  def get_authorization(self):
    """
    Step 1: Get Bearer token with cookies and issue_token
    Step 2: Use Bearer token to get an JWT access token, nestID
    """
    print("<> Getting Bearer token ...")
    headers = {
      'Sec-Fetch-Mode': 'cors',
      'User-Agent': self.user_agent,
      'X-Requested-With': 'XmlHttpRequest',
      'Referer': 'https://accounts.google.com/o/oauth2/iframe',
      'Cookie': self.config.cookies
    }

    status, resp = self.run_requests(self.config.issueToken, 'GET', headers=headers)
    access_token = ''

    if status:
      try:
        access_token = resp.json().get('access_token')
      except Exception as no_token_error:
        print("ERROR: failed to get access_token with error: \n{}".format(no_token_error))
        exit(1)
      print("<> Status: {}".format(resp.reason))
    else:
      print("<> FAILED: unable to get Bearer token.")
      exit(1)

    print("<> Getting Google JWT authorization token ...")
    headers = {
      'Referer': 'https://home.nest.com/',
      'Authorization': 'Bearer ' + access_token,
      'X-Goog-API-Key': "{}".format(self.config.apiKey),  # Nest public APIkey 'AIzaSyAdkSIMNc51XGNEAYWasX9UOWkS5P6sZE4'
      'User-Agent': "{}".format(self.user_agent),
    }
    params = {
      'embed_google_oauth_access_token': True,
      'expire_after': '3600s',
      'google_oauth_access_token': "{}".format(access_token),
      'policy_id': 'authproxy-oauth-policy'
    }

    status, resp = self.run_requests(self.nest_auth_url, method='POST', headers=headers, params=params)
    if status:
      try:
        self.nest_access_token = resp.json().get('jwt')
        # self.nest_access_token_expiration = resp.json().get('claims').get('expirationTime')
        self.nest_user_id = resp.json().get('claims').get('subject').get('nestId').get('id')
      except Exception as jwt_error:
        print("ERROR: failed to get JWT access token with error: \n{}".format(jwt_error))
        exit(1)
      print("<> Status: {}".format(resp.reason))
    else:
      print("<> FAILED: unable to get JWT authorisation token.")
      exit(1)

  def initialize_user(self):
    """Gets the assets belonging to Nest user."""

    user_url = self.nest_user_url.replace('#USERID#', self.nest_user_id)

    print("<> Getting user's nest cameras assets ...")

    headers = {
      'Authorization': "Basic {}".format(self.nest_access_token),
      'Content-Type': 'application/json'
    }

    user_object = None

    payload = self.nest_user_request_payload
    status, resp = self.run_requests(user_url, method='POST', headers=headers, payload=payload)
    if status:
      try:
        user_object = resp.json()
      except Exception as assets_error:
        print("ERROR: failed to get user's assets error: \n{}".format(assets_error))
        exit(1)
      print("<> Status: {}".format(resp.reason))

      # user_object = resp.json()
      for bucket in user_object['updated_buckets']:
        bucket_id = bucket['object_key']
        if bucket_id.startswith('quartz.'):
          print("<> INFO: Detected camera configuration.")

          # Attempt to get cameras API region
          try:
            nexus_api_http_server_url = bucket['value']['nexus_api_http_server_url']
            region = re_search('https://nexusapi-(.+?).dropcam.com', nexus_api_http_server_url).group(1)
          except AttributeError:
            # Failed to find region - default back to us1
            region = 'us1'

          global camera
          camera = {
            'name': bucket['value']['description'].replace(' ', '_'),
            'uuid': bucket_id.replace('quartz.', ''),
            'streaming_state': bucket['value']['streaming_state'],
            'region': region
          }
          # print(f"<> DEBUG: {bucket}")
          print("<> INFO: Camera Name: '{}' UUID: '{}' STATE: '{}'".format(camera['name'], camera['uuid'], camera['streaming_state']))
          self.nest_camera_array.append(camera)

  @staticmethod
  def addTimestamp(self, image_path) :
    # Add timestamp into jpg
    if self.convert_path:
      overlay_text = shsplit("{} {} -pointsize 36 -fill white "
                              "-stroke black -annotate +40+40 '{}' "
                              "{}".format(self.convert_path, image_path, self.now_time('%Y-%m-%d %H:%M:%S'), image_path))
      call(overlay_text)

  @staticmethod
  def clearImages(self, camera_buffer, camera_path, camera) :
    # If the user specified the need to remove images post-processing
    # then clear the image folder from images in the buffer.
    if self.config.clear_images:
      clean_files = []
      for buffer_entry in camera_buffer[camera['uuid']]:
        clean_files.append(os.path.join(camera_path, buffer_entry))

      print("<> INFO: Deleting {}".format(clean_files))
      call(shsplit('rm -f') + shsplit(' '.join(clean_files)))

  @staticmethod
  def handleErrors(self, response, camera_name) :
    # camera is offline
    if response.status_code == 404:
      print("<> WARNING: {} {}: recording not available.".format(self.now_time(), camera_name))
      time.sleep(self.cam_retry_wait)

    # Renew auth token
    elif response.status_code == 403:
      print("<> DEBUG: {} {}: status '{}' token expired renewing ...".format(self.now_time(), camera_name, response.reason))
      self.get_authorization()

    elif response.status_code == 500:
      sleep_time = 30
      print("<> DEBUG: {} {}: '{}' ... failure received sleeping for {} seconds.".format(self.now_time(), camera_name, response.reason))
      time.sleep(sleep_time)

    else:
      print("<> DEBUG: {} {}: Ignoring status code '{}'".format(self.now_time(), camera_name, response.status_code))

  @staticmethod
  def compileVideo(self, camera_name, camera, camera_buffer, video_path, file_id, camera_path, force_compile=False) :
    # Compile video
    if self.ffmpeg_path:
      camera_buffer_size = len(camera_buffer[camera['uuid']])
      print(
        "<> INFO: {} [ {} ] "
        "Camera buffer size for {}: {}".format(self.now_time(), threading.current_thread().name, camera_name, camera_buffer_size)
      )

      if camera_buffer_size < self.nest_camera_buffer_threshold and force_compile == False:
        camera_buffer[camera['uuid']].append(file_id)

      else:
        camera_buffer[camera['uuid']].append(file_id)
        camera_image_folder = os.path.join(self.local_path, camera_path)

        # Add the batch of .jpg files that need to be made into a video.
        file_declaration = ''
        for buffer_entry in camera_buffer[camera['uuid']]:
          file_declaration = "{}file '{}/{}'\n".format(file_declaration, camera_image_folder, buffer_entry)
        concat_file_name = os.path.join(self.temp_dir_path, camera['uuid'] + '.txt')

        # Write to file image list to be compressed into video
        with open(concat_file_name, 'w') as declaration_file:
          declaration_file.write(file_declaration)

        print("<> INFO: {} {}: Processing video!".format(self.now_time(), camera_name))
        video_file_name = "{}.mp4".format(self.now_time('%Y-%m-%d_%H-%M-%S'))
        target_video_path = os.path.join(video_path, video_file_name)

        process = Popen(
          [self.ffmpeg_path, '-r', str(self.config.frame_rate), '-f', 'concat', '-safe', '0', '-i',
            concat_file_name, '-vcodec', 'libx264', '-crf', '25', '-pix_fmt', 'yuv420p',
            target_video_path],
          close_fds=False, start_new_session=True, stdout=PIPE, stderr=PIPE
        )

        process.communicate()
        os.remove(concat_file_name)
        print("<> INFO: {} {}: Video processing is complete!".format(self.now_time(), camera_name))

        self.clearImages(self, camera_buffer, camera_path, camera)

        # Empty buffer, since we no longer need the file records that we're planning
        # to compile in a video.
        camera_buffer[camera['uuid']] = []

  def capture_images(self, capture=True):
    """Starts the multi-threaded image capture process."""

    print("<> INFO: {} Capturing images ...".format(self.now_time()))
    raise Exception()

    self.is_capturing = capture

    if not os.path.exists('capture'):
      os.makedirs('capture')

    for camera in self.nest_camera_array:
      global camera_name, camera_path, video_path
      camera_name = camera['name'] or camera['uuid']

      camera_path, video_path = self.createAndGetOutputPaths(self, camera_name)

      image_thread = threading.Thread(target=self.perform_capture,
                                      args=(camera, camera_name, camera_path, video_path))
      image_thread.start()

  def perform_capture(self, camera, camera_name, camera_path='', video_path=''):
    """Captures images and generates the video from them."""
    global camera_buffer, file_id, image_path
    camera_buffer = defaultdict(list)
    image_url = self.nest_image_url.replace('#CAMERAID#', camera['uuid']
                                            ).replace('#WIDTH#', str(self.config.width)
                                                      ).replace('#REGION#', camera['region'])

    while self.is_capturing:
      file_id = "{}.jpg".format(str(uuid.uuid4().hex))
      image_path = "{}/{}".format(camera_path, file_id)
      utc_date = datetime.utcnow()
      utc_millis_str = str(int(utc_date.timestamp())*1000)

      print("<> INFO: {} Applied cache buster: {}".format(self.now_time(), utc_millis_str))

      image_url = image_url.replace('#CBUSTER#', utc_millis_str)

      headers = {
        'Origin': 'https://home.nest.com',
        'Referer': 'https://home.nest.com/',
        'Authorization': 'Basic ' + self.nest_access_token,
        'accept': 'image/webp,image/apng,image/*,*/*;q=0.9',
        'accept-encoding': 'gzip, deflate, br',
        'user-agent': self.user_agent,
      }

      status, resp = self.run_requests(image_url, method='GET', headers=headers)

      if status:
        if resp.status_code == 200: # Check if the camera live feed is available
          try:
            time.sleep(0.5)

            with open(image_path, 'wb') as image_file:
              image_file.write(resp.content)

            self.addTimestamp(self, image_file)

            self.compileVideo(self, camera_name, camera, camera_buffer, video_path, file_id, camera_path)

          except Exception as img_error:
            print("<> ERROR: {} {}: while getting image ... \n {} \n".format(self.now_time(), camera_name, img_error))
            print("<> DEBUG: image URL {}".format(image_url))
            traceback.print_exc()
            
        else:
          self.handleErrors(self, resp, camera_name)
      else:
        print("<> ERROR: {}: failed to capture images".format(camera_name))
        exit(1)


if __name__ == '__main__':
  try:
    import json
    from collections import namedtuple
    print("Welcome to FoggyCam 1.0 - Nest video/image capture tool")

    CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'config.json'))
    print(CONFIG_PATH)

    CONFIG = json.load(open(CONFIG_PATH), object_hook=lambda d: namedtuple('X', d.keys())(*d.values()))

    CAM = FoggyCam(config=CONFIG)
  except KeyboardInterrupt:
    print("FoggyCam 1.0 - Nest video/image capture tool ended.")

  except Exception as global_error:
    print("<> CRITICAL: unknown error \n {}".format(global_error))
    username = CONFIG.email_username
    password = CONFIG.email_password

    emailsender.compose_email(['nickmartinson986@gmail.com','',''],
      'Nest Video capture crash',
      [['The Nest Video capture script crashed\n',0]],
      '',
      username,
      password);          
      # CAM.compileVideo(CAM, camera_name, camera, camera_buffer, video_path, file_id, camera_path, True)
