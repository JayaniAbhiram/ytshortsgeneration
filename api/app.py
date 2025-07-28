import os
import subprocess
import datetime
import glob
import threading
import queue
import sys
import tempfile # For temporary local file storage
from moviepy.editor import VideoFileClip, CompositeVideoClip, ColorClip
from flask import Flask, render_template, request, jsonify, url_for, send_from_directory

# Vercel Blob SDK import
from vercel_blob import put, list_blobs, del_blobs # Will need to configure BLOB_READ_WRITE_TOKEN on Vercel

app = Flask(__name__)

# --- Configuration (Stored in app.config) ---
# These directories will now be TEMPORARY local storage on the serverless function.
# The actual persistent storage will be Vercel Blob.
# We still define them as they'll be used for temporary local file operations.
TEMP_LOCAL_DOWNLOAD_DIR = tempfile.gettempdir() # Use system temp directory
TEMP_LOCAL_CLIPS_DIR = tempfile.gettempdir() # Use system temp directory

app.config['SHORT_VIDEO_WIDTH'] = 1080
app.config['SHORT_VIDEO_HEIGHT'] = 1920

app.config['CLIP_DURATION_SECONDS'] = 27
app.config['NUM_CLIPS_TO_GENERATE'] = 5

# Global queue to store logs from the processing thread
log_queue = queue.Queue()

# --- Helper Functions ---
def log_message(message):
    print(message)
    log_queue.put(message)

def run_subprocess_command(cmd_list, error_msg_prefix, cwd=None):
    try:
        process = subprocess.run(cmd_list, capture_output=True, text=True, check=True, cwd=cwd)
        return process.stdout
    except subprocess.CalledProcessError as e:
        log_message(f"{error_msg_prefix} (command exited with code {e.returncode}):")
        log_message(f"STDOUT: {e.stdout}")
        log_message(f"STDERR: {e.stderr}")
        return None
    except FileNotFoundError:
        log_message(f"Error: Command '{cmd_list[0]}' not found.")
        log_message(f"Please ensure {cmd_list[0]} is installed and in your system's PATH on Vercel's build environment.")
        return None

def download_youtube_video_to_temp(youtube_url, temp_dir):
    """Downloads a YouTube video to a temporary local path using yt-dlp."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename_template = f"full_video_{timestamp}.%(ext)s"
    output_path_template = os.path.join(temp_dir, output_filename_template)

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
        "-o", output_path_template,
        "--no-part",
        youtube_url
    ]

    log_message(f"\n--- Downloading YouTube video from: {youtube_url} ---")
    log_message(f"Saving temporarily to: {temp_dir}")

    stdout_output = run_subprocess_command(cmd, "Error downloading video with yt-dlp", cwd=temp_dir)
    if stdout_output is None:
        return None

    downloaded_filepath = None
    for line in stdout_output.splitlines():
        if "Destination:" in line:
            downloaded_filepath_from_output = line.split("Destination:")[1].strip()
            # yt-dlp might print relative path, make it absolute relative to temp_dir
            downloaded_filepath = os.path.join(temp_dir, os.path.basename(downloaded_filepath_from_output))
            if os.path.exists(downloaded_filepath):
                log_message(f"Download complete! Temp file at: {downloaded_filepath}")
                return downloaded_filepath
            break

    # Fallback: Search the temporary directory for the most recent .mp4 file
    log_message("Could not precisely determine downloaded filename from yt-dlp output. Searching temp directory...")
    list_of_files = glob.glob(os.path.join(temp_dir, f"full_video_{timestamp}*.mp4"))
    if list_of_files:
        list_of_files.sort(key=os.path.getmtime, reverse=True)
        downloaded_filepath = list_of_files[0]
        log_message(f"Download complete (found via temp dir scan)! Temp file at: {downloaded_filepath}")
        if os.path.exists(downloaded_filepath):
            return downloaded_filepath

    log_message(f"Warning: Downloaded file path '{downloaded_filepath}' not confirmed to exist in temp dir.")
    return None

def generate_short_clips_to_temp(video_path, clip_duration, num_clips, temp_output_dir):
    """Generates multiple short clips from a full video to a temporary local path."""
    log_message(f"\n--- Processing video: {os.path.basename(video_path)} ---")
    generated_temp_clip_paths = []
    try:
        full_clip = VideoFileClip(video_path)
    except Exception as e:
        log_message(f"Error loading full video clip from {video_path}: {e}")
        log_message("This might be due to a corrupted download or an unsupported video format.")
        return generated_temp_clip_paths

    video_duration = full_clip.duration
    generated_clips_count = 0

    log_message(f"Full video duration: {video_duration:.2f} seconds.")

    target_aspect = app.config['SHORT_VIDEO_WIDTH'] / app.config['SHORT_VIDEO_HEIGHT']

    for i in range(num_clips):
        start_time = i * clip_duration
        end_time = start_time + clip_duration

        if start_time >= video_duration:
            log_message(f"Reached end of video. Generated {generated_clips_count} clips.")
            break

        if end_time > video_duration:
            end_time = video_duration
            if (end_time - start_time) < (clip_duration / 2):
                log_message(f"Remaining video segment ({end_time - start_time:.2f}s) is too short. Stopping.")
                break

        log_message(f"Generating clip {i+1} from {start_time:.2f}s to {end_time:.2f}s...")

        sub_clip = full_clip.subclip(start_time, end_time)

        if sub_clip.w / sub_clip.h > target_aspect:
            new_width = int(sub_clip.h * target_aspect)
            processed_clip = sub_clip.crop(x_center=sub_clip.w/2, width=new_width)
        elif sub_clip.w / sub_clip.h < target_aspect:
            new_height = int(sub_clip.w / target_aspect)
            processed_clip = sub_clip.crop(y_center=sub_clip.h/2, height=new_height)
        else:
            processed_clip = sub_clip

        processed_clip = processed_clip.resize((app.config['SHORT_VIDEO_WIDTH'], app.config['SHORT_VIDEO_HEIGHT']))

        output_filename = f"short_clip_{i+1}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        output_filepath = os.path.join(temp_output_dir, output_filename)

        log_message(f"Rendering clip {i+1} to: {output_filepath}")
        try:
            processed_clip.write_videofile(
                output_filepath,
                codec='libx264',
                audio_codec='aac',
                fps=24,
                threads=1 # Limit threads for serverless environment
            )
            log_message(f"Clip {i+1} generated successfully (temp)! {output_filepath}")
            generated_temp_clip_paths.append(output_filepath)
            generated_clips_count += 1
        except Exception as e:
            log_message(f"Error rendering clip {i+1}: {e}")
            log_message("This often indicates an issue with FFmpeg or specific video codec problems during MoviePy rendering.")

    log_message(f"\n--- Finished generating {generated_clips_count} temporary clips. ---")
    return generated_temp_clip_paths

def upload_to_vercel_blob(local_filepath):
    """Uploads a local file to Vercel Blob and returns its public URL."""
    if not os.path.exists(local_filepath):
        log_message(f"Error: Local file '{local_filepath}' not found for upload to Vercel Blob.")
        return None

    blob_path = f"clips/{os.path.basename(local_filepath)}" # Store in a 'clips' folder in Blob
    try:
        with open(local_filepath, "rb") as f:
            log_message(f"Uploading '{os.path.basename(local_filepath)}' to Vercel Blob...")
            # Vercel Blob automatically handles multipart uploads for large files
            blob = put(blob_path, f, access='public', addRandomSuffix=True) # Add suffix to prevent overwrites
            log_message(f"Uploaded to Vercel Blob: {blob.url}")
            return blob.url
    except Exception as e:
        log_message(f"Error uploading '{local_filepath}' to Vercel Blob: {e}")
        return None

def process_video_task(youtube_link):
    """
    Main processing task to be run in a separate thread.
    Downloads, processes, uploads to Blob, and puts results/logs into the global queue.
    """
    try:
        # 1. Download video to temporary local storage
        downloaded_full_video_path = download_youtube_video_to_temp(youtube_link, TEMP_LOCAL_DOWNLOAD_DIR)

        if not downloaded_full_video_path or not os.path.exists(downloaded_full_video_path):
            log_queue.put({"status": "failed", "message": "Failed to download the YouTube video. Cannot generate clips."})
            log_message("Failed to download the YouTube video. Cannot generate clips.")
            return

        # 2. Generate clips to temporary local storage
        temp_clip_paths = generate_short_clips_to_temp(
            downloaded_full_video_path,
            app.config['CLIP_DURATION_SECONDS'],
            app.config['NUM_CLIPS_TO_GENERATE'],
            TEMP_LOCAL_CLIPS_DIR
        )

        # 3. Upload generated clips from temporary local storage to Vercel Blob
        blob_urls = []
        if temp_clip_paths:
            log_message("\n--- Uploading generated clips to Vercel Blob ---")
            for temp_path in temp_clip_paths:
                blob_url = upload_to_vercel_blob(temp_path)
                if blob_url:
                    blob_urls.append(blob_url)
                # Clean up local temporary clip file immediately after upload
                try:
                    os.remove(temp_path)
                    log_message(f"Cleaned up temporary clip file: {temp_path}")
                except OSError as e:
                    log_message(f"Error cleaning up temp clip file {temp_path}: {e}")

        # 4. Clean up the original downloaded video from temporary local storage
        try:
            os.remove(downloaded_full_video_path)
            log_message(f"Cleaned up temporary downloaded video: {downloaded_full_video_path}")
        except OSError as e:
            log_message(f"Error cleaning up temp downloaded video {downloaded_full_video_path}: {e}")

        if blob_urls:
            log_queue.put({"status": "completed", "clips": blob_urls})
        else:
            log_queue.put({"status": "failed", "message": "No clips were generated or uploaded to Vercel Blob."})

    except Exception as e:
        log_queue.put({"status": "error", "message": f"An unexpected error occurred: {e}"})
        log_message(f"An unexpected error occurred in processing task: {e}")

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate_clips', methods=['POST'])
def generate_clips():
    youtube_link = request.form['youtube_link']

    if not youtube_link or not (youtube_link.startswith("http://") or youtube_link.startswith("https://")):
        return jsonify({"status": "error", "message": "Invalid YouTube link provided."})

    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break

    log_queue.put("Starting video processing...")
    log_queue.put("NOTE: Video processing is resource-intensive and may take time. Please monitor logs.")
    log_queue.put("NOTE: Serverless functions have time limits (e.g., 5 min). Long videos might time out.")

    thread = threading.Thread(target=process_video_task, args=(youtube_link,))
    thread.start()

    return jsonify({"status": "processing", "message": "Video generation started. Check logs for updates."})

@app.route('/get_logs')
def get_logs():
    """Endpoint to fetch logs from the processing thread."""
    logs = []
    while not log_queue.empty():
        try:
            item = log_queue.get_nowait()
            if isinstance(item, dict) and "status" in item:
                return jsonify(item)
            else:
                logs.append(item)
        except queue.Empty:
            break

    return jsonify({"status": "logging", "logs": logs})

# The /download route now serves directly from Vercel Blob URLs
# There is no local file serving by Flask needed for the final clips.
# The HTML will link directly to the blob URLs.

if __name__ == '__main__':
    app.run(debug=True)