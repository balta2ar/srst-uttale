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

@app.route('/audio/<filename>')
def get_audio(filename):
    audio_file = os.path.join(media_dir, filename.replace('.vtt', '.ogg'))
    return send_file(audio_file)

if __name__ == '__main__':
    import sys
    media_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    app.run(host='0.0.0.0', port=5000)
