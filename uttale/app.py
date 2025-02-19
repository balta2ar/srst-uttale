from flask import Flask, render_template, send_file, jsonify, request
import os
import glob
import webvtt

app = Flask(__name__)
media_dir = None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/list')
def list_files():
    vtt_files = glob.glob(os.path.join(media_dir, '*.vtt'))
    return jsonify(sorted([os.path.basename(f) for f in vtt_files]))

@app.route('/search')
def search():
    query = request.args.get('q', '').lower()
    results = []
    
    for vtt_file in glob.glob(os.path.join(media_dir, '*.vtt')):
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
    import sys
    media_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    app.run(host='0.0.0.0', port=5000)
