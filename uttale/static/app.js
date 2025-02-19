let currentVtt = null;
let currentSubtitle = null;
let subtitleData = null;
let currentFileName = null;
const player = document.getElementById('player');
const searchInput = document.getElementById('search');
const fileList = document.getElementById('fileList');
const subtitles = document.getElementById('subtitles');
const autoScrollCheckbox = document.getElementById('autoScroll');

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
        `<div class="file ${file === currentFileName ? 'active' : ''}" onclick="loadVtt('${file}')">${file}</div>`
    ).join('');
}

async function loadVtt(filename) {
    currentFileName = filename;
    const response = await fetch(`/vtt/${filename}`);
    const text = await response.text();
    currentVtt = text;
    subtitleData = parseVtt(text);
    displaySubtitles(subtitleData);
    
    player.innerHTML = '';
    const source = document.createElement('source');
    source.src = `/audio/${filename}`;
    player.appendChild(source);
    player.load();
    
    displayFiles(await (await fetch('/list')).json());
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
        `<div class="subtitle" data-index="${idx}" data-start="${sub.start}" data-end="${sub.end}" onclick="playSubtitle(${idx})">${sub.text}</div>`
    ).join('');
}

function playSubtitle(idx) {
    if (currentSubtitle === idx) {
        player.paused ? player.play() : player.pause();
    } else {
        highlightSubtitle(idx);
        player.currentTime = subtitleData[idx].start;
        player.play();
        currentSubtitle = idx;
    }
}

function highlightSubtitle(idx) {
    const subs = document.querySelectorAll('.subtitle');
    subs.forEach(sub => sub.classList.remove('active'));
    if (idx !== null) {
        subs[idx].classList.add('active');
        if (autoScrollCheckbox.checked) {
            subs[idx].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
}

function findCurrentSubtitle(currentTime) {
    return subtitleData.findIndex(sub => 
        currentTime >= sub.start && currentTime <= sub.end
    );
}

player.addEventListener('timeupdate', () => {
    const currentIdx = findCurrentSubtitle(player.currentTime);
    if (currentIdx !== currentSubtitle && currentIdx !== -1) {
        currentSubtitle = currentIdx;
        highlightSubtitle(currentIdx);
    }
});

player.addEventListener('error', (e) => {
    console.error('Audio error:', player.error);
});

searchInput.addEventListener('input', e => {
    if (e.target.value.trim()) {
        searchFiles(e.target.value);
    } else {
        loadFiles();
    }
});

loadFiles();
