from flask import Flask, render_template, send_file, jsonify, request
import os
import glob
import webvtt
import argparse

def get_vtt_files(directory):
    return glob.glob(os.path.join(directory, '*.vtt'))

app = Flask(__name__)
media_dir = None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/list')
def list_files():
    return jsonify(sorted([os.path.basename(f) for f in get_vtt_files(media_dir)]))

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
            results.append(os.path.basename(vtt_file))
    
    return jsonify(sorted(results))

@app.route('/vtt/<filename>')
def get_vtt(filename):
    return send_file(os.path.join(media_dir, filename))

MIME_TYPES = {
    '.mp3': 'audio/mpeg',
    '.ogg': 'audio/ogg',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac'
}

@app.route('/audio/<filename>')
def get_audio(filename):
    base_name = filename.replace('.vtt', '')
    extensions = ['.m4a', '.aac', '.mp3', '.ogg']
    
    for ext in extensions:
        audio_file = os.path.join(media_dir, base_name + ext)
        if os.path.exists(audio_file):
            response = send_file(
                audio_file,
                mimetype=MIME_TYPES.get(ext, 'application/octet-stream')
            )
            response.headers['Accept-Ranges'] = 'bytes'
            return response
    
    return 'Audio file not found', 404

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='VTT and Audio file server')
    parser.add_argument('media_dir', help='Directory containing VTT and audio files')
    parser.add_argument('--interface', default='0.0.0.0', help='Interface to listen on (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on (default: 5000)')
    
    args = parser.parse_args()
    print(args)
    media_dir = args.media_dir
    
    vtt_count = len(get_vtt_files(media_dir))
    print(f'Found {vtt_count} VTT files in {media_dir}')
    
    app.run(host=args.interface, port=args.port)
