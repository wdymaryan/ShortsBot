import os
import json
import requests
import subprocess
import random
import time
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# --- Environment Variables ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN")

genai.configure(api_key=GEMINI_API_KEY)

# --- 1. Ledger & API Auto-Retries ---
def get_past_topics():
    if not os.path.exists("past_topics.txt"):
        return ""
    with open("past_topics.txt", "r", encoding="utf-8") as f:
        return f.read()

def generate_short_payload(retries=3):
    past_topics = get_past_topics()
    with open("system_prompt.txt", "r", encoding="utf-8") as f:
        sys_prompt = f.read()

    # Model cascade to prevent 404 deprecation errors
    candidate_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]

    for model_name in candidate_models:
        for attempt in range(retries):
            try:
                print(f"Querying {model_name} (Attempt {attempt+1})...")
                model = genai.GenerativeModel(
                    model_name=model_name,
                    system_instruction=sys_prompt,
                    generation_config={"response_mime_type": "application/json"}
                )
                res = model.generate_content(f"Generate a new Short. PAST_TOPICS:\n{past_topics}")
                return json.loads(res.text)
            except Exception as e:
                print(f"Error on {model_name} (Attempt {attempt+1}): {e}")
                time.sleep(3)

    raise Exception("Critical: Failed to generate script across all available Gemini models.")

# --- 2. Convert VTT Subtitles to Styled SRT ---
def vtt_to_srt(vtt_path, srt_path):
    with open(vtt_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    srt_lines = []
    sub_index = 1
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            times = line.replace(".", ",")
            i += 1
            text = lines[i].strip() if i < len(lines) else ""
            if text:
                srt_lines.append(f"{sub_index}\n{times}\n{text.upper()}\n\n")
                sub_index += 1
        i += 1
        
    with open(srt_path, "w", encoding="utf-8") as f:
        f.writelines(srt_lines)

# --- 3. Edge-TTS Audio Generation ---
def generate_voiceover(text, voice, output_audio, output_vtt):
    cmd = [
        "edge-tts",
        "--text", text,
        "--voice", voice,
        "--write-media", output_audio,
        "--write-subtitles", output_vtt
    ]
    subprocess.run(cmd, check=True)

# --- 4. Fetch Pixabay B-Roll Video ---
def get_pixabay_video(fallbacks, output_file, retries=3):
    safe_fallbacks = fallbacks + ["supermarket", "shopping", "abstract background", "dark texture"]
    
    for attempt in range(retries):
        for term in safe_fallbacks:
            try:
                query = term.replace(" ", "+")
                url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&min_width=720"
                res = requests.get(url, timeout=15).json()
                
                if res.get("totalHits", 0) > 0:
                    videos = res["hits"][0]["videos"]
                    selected = videos.get("large") or videos.get("medium") or videos.get("small") or videos.get("tiny")
                    video_url = selected["url"]
                    
                    vid_data = requests.get(video_url, timeout=25).content
                    with open(output_file, "wb") as f:
                        f.write(vid_data)
                    print(f"Downloaded B-roll for query: '{term}'")
                    return True
            except Exception as e:
                print(f"Pixabay fetch error on '{term}': {e}")
                time.sleep(2)

    raise Exception("Critical: Failed to download video after trying all fallback queries.")

# --- 5. Pure FFmpeg Video Assembly ---
def render_scene(video_in, audio_in, srt_in, scene_num):
    out_scene = f"scene_{scene_num}.mp4"
    
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_in
    ]).decode().strip()
    duration = float(probe)

    # Subtitle overlay styling
    sub_style = "FontName=DejaVu Sans,FontSize=20,Bold=1,PrimaryColour=&H00FFFF,OutlineColour=&H000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=120"
    vf_string = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles={srt_in}:force_style='{sub_style}'"

    cmd = [
        "ffmpeg", "-y", "-i", video_in, "-i", audio_in,
        "-t", str(duration),
        "-vf", vf_string,
        "-c:v", "libx264", "-c:a", "aac", "-shortest", out_scene
    ]
    subprocess.run(cmd, check=True)
    return out_scene

def build_final_video(scene_files, output_final):
    with open("concat.txt", "w", encoding="utf-8") as f:
        for s in scene_files:
            f.write(f"file '{s}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
        "-c", "copy", "raw_concat.mp4"
    ], check=True)

    bgm_dir = "bgm"
    has_bgm = os.path.exists(bgm_dir) and any(f.endswith(".mp3") for f in os.listdir(bgm_dir))
    
    if has_bgm:
        valid_tracks = [os.path.join(bgm_dir, f) for f in os.listdir(bgm_dir) if f.endswith(".mp3")]
        random_bgm = random.choice(valid_tracks)
        print(f"Applying background track: {random_bgm}")
        cmd = [
            "ffmpeg", "-y", "-i", "raw_concat.mp4", "-stream_loop", "-1", "-i", random_bgm,
            "-filter_complex", "[1:a]volume=0.10[bgm];[0:a][bgm]amix=inputs=2:duration=first[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", output_final
        ]
    else:
        print("No BGM track found. Exporting voiceover only.")
        cmd = ["ffmpeg", "-y", "-i", "raw_concat.mp4", "-c", "copy", output_final]
        
    subprocess.run(cmd, check=True)

# --- 6. YouTube Upload ---
def upload_to_youtube(video_path, metadata):
    creds = Credentials(
        None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET
    )
    youtube = build("youtube", "v3", credentials=creds)

    title_str = metadata.get("title", "Consumer Psychology Trick")
    if "#shorts" not in title_str.lower():
        title_str = f"{title_str[:60]} #shorts"

    body = {
        "snippet": {
            "title": title_str,
            "description": metadata.get("description", ""),
            "tags": metadata.get("tags", ["shorts", "psychology"]),
            "categoryId": "27"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    try:
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = request.execute()
        video_id = response.get("id")
        print(f"Upload complete! Video ID: {video_id}")

        pinned_text = metadata.get("pinned_comment")
        if pinned_text:
            comment_body = {
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {"textOriginal": pinned_text}
                    }
                }
            }
            youtube.commentThreads().insert(part="snippet", body=comment_body).execute()
            print("Pinned comment published.")
    except HttpError as e:
        print(f"YouTube Data API error: {e}")
        raise e

# --- 7. Temporary File Cleanup ---
def cleanup_files(scene_count):
    files_to_delete = ["concat.txt", "raw_concat.mp4"]
    for i in range(scene_count):
        files_to_delete.extend([
            f"audio_{i}.mp3", f"video_{i}.mp4", f"sub_{i}.srt",
            f"sub_{i}.vtt", f"scene_{i}.mp4"
        ])
    
    for f in files_to_delete:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass
    print("Cleaned up temporary scene files.")

# --- Execution Entry Point ---
def main():
    print("1. Requesting production package from Gemini...")
    data = generate_short_payload()
    
    topic_id = data.get("topic_id", f"topic_{int(time.time())}")
    with open("past_topics.txt", "a", encoding="utf-8") as f:
        f.write(topic_id + "\n")

    scenes = data.get("scenes", [])
    voice_id = data.get("production_config", {}).get("voice_id", "en-US-ChristopherNeural")

    scene_files = []
    for i, scene in enumerate(scenes):
        print(f"2. Processing Scene {i+1}/{len(scenes)}...")
        a_file = f"audio_{i}.mp3"
        vtt_file = f"sub_{i}.vtt"
        srt_file = f"sub_{i}.srt"
        v_file = f"video_{i}.mp4"
        
        spoken_text = scene.get("spoken_script", "")
        generate_voiceover(spoken_text, voice_id, a_file, vtt_file)
        vtt_to_srt(vtt_file, srt_file)
        
        fallbacks = scene.get("pexels_search_fallbacks") or scene.get("pixabay_search_fallbacks") or ["store", "money"]
        get_pixabay_video(fallbacks, v_file)
        
        scene_mp4 = render_scene(v_file, a_file, srt_file, i)
        scene_files.append(scene_mp4)

    print("3. Stitching video and mixing audio tracks...")
    build_final_video(scene_files, "final_short.mp4")

    print("4. Uploading directly to YouTube channel...")
    upload_to_youtube("final_short.mp4", data.get("youtube_metadata", {}))
    
    cleanup_files(len(scenes))
    print("Workflow execution completed successfully.")

if __name__ == "__main__":
    main()
        
