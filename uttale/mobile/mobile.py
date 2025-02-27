import argparse
import hashlib
import logging
import re
import subprocess
import tempfile
from glob import glob
from os import path
from pathlib import Path

import webvtt
from flask import Flask, jsonify, render_template, request, send_file


def get_vtt_files(directory):
    return glob(path.join(directory, '*.vtt'))

app = Flask(__name__)
media_dir = None
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('mobile')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/list')
def list_files():
    return jsonify(sorted([path.basename(f) for f in get_vtt_files(media_dir)]))

@app.route('/search')
def search():
    query = request.args.get('q', '').lower()
    results = []

    for vtt_file in get_vtt_files(media_dir):
        vtt = webvtt.read(vtt_file)
        found = False
        for caption in vtt:
            if query in caption.text.lower():
                found = True
                break
        if found:
            results.append(path.basename(vtt_file))

    return jsonify(sorted(results))

@app.route('/vtt/<filename>')
def get_vtt(filename):
    return send_file(path.join(media_dir, filename))

MIME_TYPES = {
    '.mp3': 'audio/mpeg',
    '.ogg': 'audio/ogg',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac'
}

def ensure_temp(suffix: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / 'srst_uttale' / suffix
    temp_dir.mkdir(parents=True, exist_ok=True)
    return str(temp_dir)


def convert_audio(input_file: str) -> str:
    temp_dir = ensure_temp('audio')
    
    file_hash = hashlib.md5(input_file.encode()).hexdigest()
    basename = path.splitext(path.basename(input_file))[0]
    output_file = path.join(temp_dir, f"{basename}_{file_hash}.mp3")
    
    if path.exists(output_file):
        logger.info(f'Using previously converted file: {output_file}')
        return output_file
        
    logger.info(f'Converting {input_file} to {output_file}')
    try:
        subprocess.run(
            ['ffmpeg', '-i', input_file, '-codec:a', 'libmp3lame', '-qscale:a', '2', output_file],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return output_file
    except subprocess.CalledProcessError as e:
        logger.error(f'Error converting audio: {e}')
        return input_file

ios_pattern = re.compile(r'(iPhone|iPad|iPod)')
def is_ios_client(user_agent):
    return bool(ios_pattern.search(user_agent))

@app.route('/audio/<filename>')
def get_audio(filename):
    base_name = filename.replace('.vtt', '')
    extensions = ['.m4a', '.aac', '.mp3', '.ogg']
    user_agent = request.headers.get('User-Agent', '')

    for ext in extensions:
        audio_file = path.join(media_dir, base_name + ext)
        if path.exists(audio_file):
            if is_ios_client(user_agent):
                logger.info(f'Converting for iOS: {audio_file}')
                audio_file, ext = convert_audio(audio_file), '.mp3'

            logger.info(f'Serving audio file: {audio_file}')
            response = send_file(
                audio_file,
                mimetype=MIME_TYPES.get(ext, 'application/octet-stream')
            )
            response.headers['Accept-Ranges'] = 'bytes'
            return response

    return 'Audio file not found', 404

def main():
    parser = argparse.ArgumentParser(description='VTT and Audio file server')
    parser.add_argument('media_dir', help='Directory containing VTT and audio files')
    parser.add_argument('--interface', default='0.0.0.0', help='Interface to listen on (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on (default: 5000)')

    args = parser.parse_args()
    print(args)
    global media_dir
    media_dir = path.abspath(args.media_dir)
    print(f'Using media directory: {media_dir}')

    vtt_count = len(get_vtt_files(media_dir))
    print(f'Found {vtt_count} VTT files in {media_dir}')

    app.run(host=args.interface, port=args.port)

if __name__ == '__main__':
    main()
