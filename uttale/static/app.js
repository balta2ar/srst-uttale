let currentVtt = null;
let currentSubtitle = null;
const player = document.getElementById('player');
const searchInput = document.getElementById('search');
const fileList = document.getElementById('fileList');
const subtitles = document.getElementById('subtitles');

async function loadFiles() {
    const response = await fetch('/list');
    const files = await response.json();
    displayFiles(files);
}

async function searchFiles(query) {
    const response = await fetch(`/search?q=${encodeURIComponent(query)}`);
    const files = await response.json();
    displayFiles(files);
}

function displayFiles(files) {
    fileList.innerHTML = files.map(file => 
        `<div class="file" onclick="loadVtt('${file}')">${file}</div>`
    ).join('');
}

async function loadVtt(filename) {
    const response = await fetch(`/vtt/${filename}`);
    const text = await response.text();
    currentVtt = text;
    const parsedSubs = parseVtt(text);
    displaySubtitles(parsedSubs);
    player.src = `/audio/${filename.replace('.vtt', '.ogg')}`;
}

function parseVtt(vttText) {
    const parsed = [];
    const blocks = vttText.split('\n\n').filter(block => block.includes('-->'));
    
    for (const block of blocks) {
        const lines = block.trim().split('\n');
        if (lines.length < 2) continue;
        
        const timeLine = lines.find(line => line.includes('-->'));
        if (!timeLine) continue;

        const [start, end] = timeLine.split(' --> ');
        const text = lines.slice(lines.indexOf(timeLine) + 1).join('\n');
        
        parsed.push({
            start: timeToSeconds(start),
            end: timeToSeconds(end),
            text: text
        });
    }
    return parsed;
}

function timeToSeconds(timeString) {
    const [t, ms] = timeString.split('.');
    const [h, m, s] = t.split(':').map(Number);
    return h * 3600 + m * 60 + s + Number(`0.${ms}`);
}

function displaySubtitles(subs) {
    subtitles.innerHTML = subs.map((sub, idx) =>
        `<div class="subtitle" data-index="${idx}" onclick="playSubtitle(${idx})">${sub.text}</div>`
    ).join('');
}

function playSubtitle(idx) {
    const subs = document.querySelectorAll('.subtitle');
    if (currentSubtitle === idx) {
        player.paused ? player.play() : player.pause();
    } else {
        subs.forEach(sub => sub.classList.remove('active'));
        subs[idx].classList.add('active');
        const captionData = parseVtt(currentVtt)[idx];
        player.currentTime = captionData.start;
        player.play();
        currentSubtitle = idx;
    }
}

searchInput.addEventListener('input', e => {
    if (e.target.value.trim()) {
        searchFiles(e.target.value);
    } else {
        loadFiles();
    }
});

loadFiles();
