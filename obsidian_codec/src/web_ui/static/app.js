document.addEventListener('DOMContentLoaded', () => {
  // Generate or retrieve Tab Session ID
  const sessionId = (() => {
    let id = sessionStorage.getItem('obsidian_session_id');
    if (!id) {
      id = 'sess_' + Math.random().toString(36).substring(2, 15) + '_' + Date.now();
      sessionStorage.setItem('obsidian_session_id', id);
    }
    return id;
  })();

  // HTML escaping helper
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[c]);
  }

  // CSRF token storage
  let csrfToken = '';
  async function fetchCsrfToken() {
    try {
      const response = await fetch('/api/csrf');
      const data = await response.json();
      csrfToken = data.token;
    } catch (e) {
      console.error("Failed to fetch CSRF token", e);
    }
  }

  // Application State
  let sourceFilePath = null;
  let isLocalMode = true;
  let activeTab = 'tab-convert';
  let activeJobId = null;
  let pollInterval = null;
  let probedData = null;

  // Batch Conversion State
  let batchQueue = [];      // Array of file paths to process
  let batchIndex = 0;      // Current index in batch conversion
  let batchResults = [];    // Array of { name, path, size, status, error }
  let isBatchMode = false;  // Whether we are doing a batch run
  let currentBasePayload = null;
  let batchStartTime = null;

  function formatRemainingTime(secs) {
    if (secs < 60) {
      return `~${Math.round(secs)}s remaining`;
    }
    const mins = Math.round(secs / 60);
    if (mins < 60) {
      return `~${mins} min remaining`;
    }
    const hrs = Math.floor(mins / 60);
    const remMins = mins % 60;
    return `~${hrs}h ${remMins}m remaining`;
  }

  // DOM Elements
  const btnLocalMode = document.getElementById('btn-local-mode');
  const btnUploadMode = document.getElementById('btn-upload-mode');
  const localPathContainer = document.getElementById('local-path-container');
  const webUploadContainer = document.getElementById('web-upload-container');
  const localPathInput = document.getElementById('local-path-input');
  const btnAnalyzeLocal = document.getElementById('btn-analyze-local');

  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');
  const uploadProgressContainer = document.getElementById('upload-progress-container');
  const uploadFilename = document.getElementById('upload-filename');
  const uploadPercentage = document.getElementById('upload-percentage');
  const uploadProgressFill = document.getElementById('upload-progress-fill');

  const analysisPanel = document.getElementById('analysis-panel');
  const settingsPanel = document.getElementById('settings-panel');
  const specDuration = document.getElementById('spec-duration');
  const specSize = document.getElementById('spec-size');
  const specContainer = document.getElementById('spec-container');
  const videoStreamsList = document.getElementById('video-streams-list');
  const audioStreamsList = document.getElementById('audio-streams-list');
  const audioChannelsGroup = document.getElementById('audio-channels-group');
  const subtitleStreamsList = document.getElementById('subtitle-streams-list');

  const tabBtns = document.querySelectorAll('.config-tab-btn');
  const tabContents = document.querySelectorAll('.tab-pane');
  const convCrf = document.getElementById('conv-crf');
  const crfValue = document.getElementById('crf-value');

  // Tab dynamic visibility fields
  const framesMode = document.getElementById('frames-mode');
  const groupTimestamp = document.getElementById('group-timestamp');
  const groupDuration = document.getElementById('group-duration');
  const groupFps = document.getElementById('group-fps');
  const lblFps = document.getElementById('lbl-fps');

  const mergeType = document.getElementById('merge-type');
  const groupMergeAudioMode = document.getElementById('group-merge-audio-mode');
  const groupMergeSubMode = document.getElementById('group-merge-sub-mode');
  const lblMergeFile = document.getElementById('lbl-merge-file');

  const extractElementType = document.getElementById('extract-element-type');
  const extractSubFormatGroup = document.getElementById('extract-sub-format-group');

  const customOutputPath = document.getElementById('custom-output-path');
  const btnStartProcess = document.getElementById('btn-start-process');

  // Modals
  const processingModal = document.getElementById('processing-modal');
  const loadingTxtLabel = document.getElementById('loading-txt-label');
  const statusPercent = document.getElementById('status-percent');
  const statusSpeed = document.getElementById('status-speed');
  const statusEta = document.getElementById('status-eta');
  const statusSize = document.getElementById('status-size');
  const consoleOutput = document.getElementById('console-output');
  const btnCancelProcess = document.getElementById('btn-cancel-process');

  const completionModal = document.getElementById('completion-modal');
  const completePath = document.getElementById('complete-path');
  const completeSize = document.getElementById('complete-size');
  const btnOpenFolder = document.getElementById('btn-open-folder');
  const btnDownloadFile = document.getElementById('btn-download-file');
  const btnCompleteClose = document.getElementById('btn-complete-close');

  const errorModal = document.getElementById('error-modal');
  const errorDetailsBox = document.getElementById('error-details-box');
  const btnErrorClose = document.getElementById('btn-error-close');

  // --- 1. Mode Switching ---
  btnLocalMode.addEventListener('click', () => {
    isLocalMode = true;
    btnLocalMode.classList.add('active');
    btnUploadMode.classList.remove('active');
    localPathContainer.classList.add('active');
    webUploadContainer.classList.remove('active');
    resetInputs();
  });

  btnUploadMode.addEventListener('click', () => {
    isLocalMode = false;
    btnUploadMode.classList.add('active');
    btnLocalMode.classList.remove('active');
    localPathContainer.classList.remove('active');
    webUploadContainer.classList.add('active');
    resetInputs();
  });

  function resetInputs() {
    sourceFilePath = null;
    probedData = null;
    batchQueue = [];
    isBatchMode = false;
    const existing = document.getElementById('batch-list-box');
    if (existing) existing.remove();
    
    document.getElementById('analysis-placeholder').classList.remove('hidden');
    document.getElementById('analysis-results').classList.add('hidden');
    localPathInput.value = '';
    fileInput.value = '';
    uploadProgressContainer.classList.add('hidden');
    uploadProgressFill.style.width = '0%';
  }

  // --- 2. Local File Analysis ---
  btnAnalyzeLocal.addEventListener('click', () => {
    const filepath = localPathInput.value.trim();
    if (!filepath) {
      showError("Please enter a file path.", "File path cannot be empty.");
      return;
    }
    analyzeFile(filepath);
  });

  async function analyzeFile(filepath) {
    btnAnalyzeLocal.disabled = true;
    const span = btnAnalyzeLocal.querySelector('span');
    if (span) span.textContent = "Analyzing...";
    
    try {
      const response = await fetch('/api/analyze', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken
        },
        body: JSON.stringify({ filepath })
      });
      
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "Failed to analyze file.");
      }
      
      if (data.is_batch) {
        batchQueue = data.files;
        isBatchMode = true;
        
        // Analyze the first file in the batch dynamically to fetch track structures
        const firstFile = data.files[0];
        const subResponse = await fetch('/api/analyze', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrfToken
          },
          body: JSON.stringify({ filepath: firstFile })
        });
        const subData = await subResponse.json();
        if (!subResponse.ok) {
          throw new Error(subData.error || "Failed to analyze prototype file.");
        }
        
        sourceFilePath = firstFile;
        probedData = subData;
        displayMetadata(subData);
        
        renderBatchListDisplay();
      } else {
        batchQueue = [];
        isBatchMode = false;
        const existing = document.getElementById('batch-list-box');
        if (existing) existing.remove();
        
        sourceFilePath = filepath;
        probedData = data;
        displayMetadata(data);
      }
      
    } catch (err) {
      showError("Analysis Failed", err.message);
    } finally {
      btnAnalyzeLocal.disabled = false;
      if (span) span.textContent = "Analyze";
    }
  }

  function renderBatchListDisplay() {
    const analysisResults = document.getElementById('analysis-results');
    const existing = document.getElementById('batch-list-box');
    if (existing) existing.remove();
    
    if (isBatchMode && batchQueue.length > 0) {
      const box = document.createElement('div');
      box.id = 'batch-list-box';
      box.className = 'track-section';
      box.innerHTML = `
        <div class="section-title">Batch Queue List (${batchQueue.length} files)</div>
        <div class="track-list" style="max-height: 150px; overflow-y: auto; background: rgba(0,0,0,0.2); padding: 10px; border-radius: 10px; border: 1px solid var(--glass-border);">
          ${batchQueue.map((f, i) => `
            <div style="font-size: 12px; padding: 6px 10px; border-bottom: 1px dashed rgba(255,255,255,0.05); color: var(--text-dim); display: flex; justify-content: space-between;">
              <span>#${i + 1}: ${escapeHtml(f.replace(/\\/g, '/').split('/').pop())}</span>
              <span style="font-family: var(--font-mono); color: var(--neon-cyan); font-size: 10px;">QUEUED</span>
            </div>
          `).join('')}
        </div>
      `;
      const scroller = analysisResults.querySelector('.specs-scroller');
      if (scroller) {
        scroller.insertBefore(box, scroller.firstChild);
      }
    }
  }

  // --- 3. Web Upload Logic (Chunked) ---
  dropZone.addEventListener('click', () => fileInput.click());

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
  });

  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) {
      handleMultipleUploads(e.dataTransfer.files);
    }
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files.length > 0) {
      handleMultipleUploads(fileInput.files);
    }
  });

  let uploadQueue = [];
  let currentUploadIndex = 0;
  let uploadedPaths = [];

  function handleMultipleUploads(files) {
    uploadQueue = Array.from(files);
    currentUploadIndex = 0;
    uploadedPaths = [];
    if (uploadQueue.length === 0) return;
    
    batchQueue = [];
    isBatchMode = false;
    
    uploadNextQueuedFile();
  }

  function uploadNextQueuedFile() {
    if (currentUploadIndex >= uploadQueue.length) {
      uploadProgressContainer.classList.add('hidden');
      batchQueue = [...uploadedPaths];
      isBatchMode = true;
      analyzeFile(batchQueue[0]);
      return;
    }
    
    const file = uploadQueue[currentUploadIndex];
    uploadProgressContainer.classList.remove('hidden');
    uploadFilename.textContent = `[Upload ${currentUploadIndex + 1}/${uploadQueue.length}] ${file.name}`;
    uploadPercentage.textContent = "0%";
    uploadProgressFill.style.width = '0%';

    const CHUNK_SIZE = 5 * 1024 * 1024;
    const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
    const identifier = `${file.size}-${file.name.replace(/[^0-9a-zA-Z]/g, '')}-${Date.now()}`;
    let currentChunk = 1;

    uploadChunk();

    function uploadChunk() {
      const start = (currentChunk - 1) * CHUNK_SIZE;
      const end = Math.min(start + CHUNK_SIZE, file.size);
      const chunk = file.slice(start, end);

      const formData = new FormData();
      formData.append('file', chunk);
      formData.append('resumableChunkNumber', currentChunk);
      formData.append('resumableTotalChunks', totalChunks);
      formData.append('resumableIdentifier', identifier);
      formData.append('resumableFilename', file.name);
      formData.append('session_id', sessionId);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload', true);
      xhr.setRequestHeader('X-CSRF-Token', csrfToken);

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const chunkProgress = (e.loaded / e.total);
          const totalProgress = ((currentChunk - 1) + chunkProgress) / totalChunks;
          const percent = Math.min(Math.round(totalProgress * 100), 99);
          uploadPercentage.textContent = `${percent}%`;
          uploadProgressFill.style.width = `${percent}%`;
        }
      };

      xhr.onload = () => {
        if (xhr.status === 200) {
          const response = JSON.parse(xhr.responseText);
          if (response.status === 'completed') {
            uploadPercentage.textContent = "100%";
            uploadProgressFill.style.width = "100%";
            uploadedPaths.push(response.filepath);
            currentUploadIndex++;
            setTimeout(uploadNextQueuedFile, 200);
          } else {
            currentChunk++;
            uploadChunk();
          }
        } else {
          showError("Upload Failed", `Failed to upload ${file.name}`);
        }
      };

      xhr.onerror = () => {
        showError("Upload Interrupted", "Check network connection and try again.");
      };

      xhr.send(formData);
    }
  }

  // --- 4. Display Metadata ---
  function displayMetadata(data) {
    const secs = parseInt(data.duration);
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    specDuration.textContent = `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    
    const mb = data.size / (1024 * 1024);
    specSize.textContent = `${mb.toFixed(2)} MB`;
    
    specContainer.textContent = data.format_long_name.split('/')[0] || data.format_name.toUpperCase();

    // (Static source preview has been removed as requested)

    // Populate video streams
    videoStreamsList.innerHTML = '';
    if (data.video_streams.length === 0) {
      videoStreamsList.innerHTML = '<p class="helper-text">No video streams detected.</p>';
    } else {
      data.video_streams.forEach(stream => {
        const div = document.createElement('div');
        div.className = 'stream-item active';
        div.innerHTML = `
          <div class="stream-details">
            <span class="stream-title">${escapeHtml(stream.display_name)}</span>
            <span class="stream-meta">Resolution: ${stream.width}x${stream.height} | FPS: ${stream.r_frame_rate} | Codec: ${escapeHtml(stream.codec_long_name)}</span>
          </div>
          <span class="stream-badge">Video</span>
        `;
        videoStreamsList.appendChild(div);
      });
    }

    // Populate audio streams
    audioStreamsList.innerHTML = '';
    if (data.audio_streams.length === 0) {
      audioStreamsList.innerHTML = '<p class="helper-text">No audio streams detected.</p>';
      audioChannelsGroup.classList.add('hidden');
    } else {
      audioChannelsGroup.classList.remove('hidden');
      data.audio_streams.forEach((stream, idx) => {
        const div = document.createElement('div');
        div.className = idx === 0 ? 'stream-item active' : 'stream-item';
        div.innerHTML = `
          <input type="radio" name="audio-stream-radio" value="${stream.index}" ${idx === 0 ? 'checked' : ''} class="stream-radio">
          <div class="stream-details">
            <span class="stream-title">${escapeHtml(stream.display_name)}</span>
            <span class="stream-meta">Channels: ${escapeHtml(stream.channel_layout)} (${stream.channels} ch) | Sample Rate: ${stream.sample_rate}Hz</span>
          </div>
          <span class="stream-badge">Audio</span>
        `;
        div.addEventListener('click', (e) => {
          if (e.target.tagName !== 'INPUT') {
            div.querySelector('input').checked = true;
          }
          document.querySelectorAll('input[name="audio-stream-radio"]').forEach(rad => {
            rad.closest('.stream-item').classList.toggle('active', rad.checked);
          });
          updateCodecOptionsForFormat();
          checkTranscodingCompatibility();
        });
        audioStreamsList.appendChild(div);
      });
    }

    // Populate subtitle streams
    subtitleStreamsList.innerHTML = '';
    if (data.subtitle_streams.length === 0) {
      subtitleStreamsList.innerHTML = '<p class="helper-text">No subtitle streams detected.</p>';
    } else {
      const noneDiv = document.createElement('div');
      noneDiv.className = 'stream-item active';
      noneDiv.innerHTML = `
        <input type="radio" name="sub-stream-radio" value="-1" checked class="stream-radio">
        <div class="stream-details">
          <span class="stream-title">Disable Subtitles</span>
          <span class="stream-meta">Do not embed or copy subtitles</span>
        </div>
      `;
      noneDiv.addEventListener('click', (e) => {
        if (e.target.tagName !== 'INPUT') noneDiv.querySelector('input').checked = true;
        updateSubActiveStates();
      });
      subtitleStreamsList.appendChild(noneDiv);

      data.subtitle_streams.forEach(stream => {
        const div = document.createElement('div');
        div.className = 'stream-item';
        div.innerHTML = `
          <input type="radio" name="sub-stream-radio" value="${stream.index}" class="stream-radio">
          <div class="stream-details">
            <span class="stream-title">${escapeHtml(stream.display_name)}</span>
            <span class="stream-meta">Codec: ${escapeHtml(stream.codec_name.toUpperCase())}</span>
          </div>
          <span class="stream-badge">Subtitle</span>
        `;
        div.addEventListener('click', (e) => {
          if (e.target.tagName !== 'INPUT') div.querySelector('input').checked = true;
          updateSubActiveStates();
        });
        subtitleStreamsList.appendChild(div);
      });
    }

    function updateSubActiveStates() {
      document.querySelectorAll('input[name="sub-stream-radio"]').forEach(rad => {
        rad.closest('.stream-item').classList.toggle('active', rad.checked);
      });
    }

    // Reveal Results and Hide Placeholder
    document.getElementById('analysis-placeholder').classList.add('hidden');
    document.getElementById('analysis-results').classList.remove('hidden');
    
    updateCodecOptionsForFormat();
    checkTranscodingCompatibility();
    settingsPanel.scrollIntoView({ behavior: 'smooth' });
  }

  // --- 5. Tabs Management ---
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.remove('active'));
      tabContents.forEach(c => c.classList.remove('active'));
      
      btn.classList.add('active');
      activeTab = btn.getAttribute('data-tab');
      document.getElementById(activeTab).classList.add('active');
    });
  });

  convCrf.addEventListener('input', () => {
    crfValue.textContent = convCrf.value;
  });

  // --- Transcoding Compatibility Verification ---
  const COMPATIBILITY_MATRIX = {
    webm: {
      video: ['libvpx', 'libvpx-vp9', 'libaom-av1', 'copy'],
      audio: ['libvorbis', 'libopus', 'copy', 'none'],
      notes: "WebM container only supports VP8, VP9, and AV1 video codecs, and Vorbis and Opus audio codecs."
    },
    ogg: {
      video: ['none', 'copy'],
      audio: ['libvorbis', 'libopus', 'flac', 'copy', 'none'],
      notes: "Ogg container only supports Vorbis, Opus, and FLAC audio. Video stream encoding to Ogg is not supported."
    },
    flv: {
      video: ['libx264', 'mpeg4', 'copy'],
      audio: ['aac', 'libmp3lame', 'pcm_s16le', 'copy', 'none'],
      notes: "FLV container does not support modern codecs like HEVC (libx265), VP9, AV1, or audio codecs like Opus, FLAC, AC3, ALAC, or Vorbis."
    },
    ts: {
      video: ['libx264', 'libx265', 'mpeg4', 'copy'],
      audio: ['aac', 'libmp3lame', 'ac3', 'copy', 'none'],
      notes: "MPEG-TS container does not support VP9, AV1, VP8, ProRes video, or Opus, FLAC, Vorbis, ALAC audio."
    },
    avi: {
      video: ['libx264', 'mpeg4', 'libxvid', 'copy'],
      audio: ['libmp3lame', 'ac3', 'pcm_s16le', 'copy', 'none'],
      notes: "AVI container does not support HEVC (libx265), VP9, AV1, ProRes, VP8 video, or AAC, Opus, FLAC, Vorbis, ALAC audio."
    },
    mov: {
      video: ['libx264', 'libx265', 'prores', 'mpeg4', 'libxvid', 'libvpx-vp9', 'libaom-av1', 'copy'],
      audio: ['aac', 'libmp3lame', 'alac', 'pcm_s16le', 'ac3', 'flac', 'copy', 'none'],
      notes: "QuickTime MOV supports H.264, HEVC, ProRes, MPEG-4, VP9, AV1 video, and AAC, MP3, ALAC, PCM, AC3, FLAC audio. VP8 video and Opus/Vorbis audio are not supported."
    },
    mp4: {
      video: ['libx264', 'libx265', 'libvpx-vp9', 'libaom-av1', 'mpeg4', 'libxvid', 'copy'],
      audio: ['aac', 'libmp3lame', 'libopus', 'flac', 'ac3', 'alac', 'libvorbis', 'pcm_s16le', 'copy', 'none'],
      notes: "MP4 container does not support ProRes or VP8 video."
    },
    m4v: {
      video: ['libx264', 'libx265', 'libvpx-vp9', 'libaom-av1', 'mpeg4', 'libxvid', 'copy'],
      audio: ['aac', 'libmp3lame', 'libopus', 'flac', 'ac3', 'alac', 'libvorbis', 'pcm_s16le', 'copy', 'none'],
      notes: "M4V container does not support ProRes or VP8 video."
    }
  };

  function mapSourceCodecToOption(codecName, type) {
    if (type === 'video') {
      if (codecName === 'h264') return 'libx264';
      if (codecName === 'hevc') return 'libx265';
      if (codecName === 'vp9') return 'libvpx-vp9';
      if (codecName === 'av1') return 'libaom-av1';
      if (codecName === 'prores') return 'prores';
      if (codecName === 'mpeg4') return 'mpeg4';
      if (codecName === 'vp8') return 'libvpx';
      if (codecName === 'xvid') return 'libxvid';
      return codecName;
    } else {
      if (codecName === 'mp3') return 'libmp3lame';
      if (codecName === 'vorbis') return 'libvorbis';
      if (codecName === 'opus') return 'libopus';
      return codecName;
    }
  }

  const CODEC_OPTION_CATALOG = {
    'conv-vcodec': Array.from(document.getElementById('conv-vcodec').options).map(option => ({
      value: option.value,
      text: option.textContent
    })),
    'conv-acodec': Array.from(document.getElementById('conv-acodec').options).map(option => ({
      value: option.value,
      text: option.textContent
    }))
  };

  function selectedAudioStream() {
    if (!probedData || !probedData.audio_streams || probedData.audio_streams.length === 0) {
      return null;
    }

    const checkedAudio = document.querySelector('input[name="audio-stream-radio"]:checked');
    return checkedAudio
      ? probedData.audio_streams.find(stream => stream.index == checkedAudio.value)
      : probedData.audio_streams[0];
  }

  function codecValueForCompatibility(codec, type) {
    if (codec !== 'copy') {
      return codec;
    }

    if (type === 'video' && probedData && probedData.video_streams && probedData.video_streams.length > 0) {
      return mapSourceCodecToOption(probedData.video_streams[0].codec_name, 'video');
    }

    const audioStream = selectedAudioStream();
    if (type === 'audio' && audioStream) {
      return mapSourceCodecToOption(audioStream.codec_name, 'audio');
    }

    return codec;
  }

  function isCodecOptionCompatible(format, codec, type) {
    const rules = COMPATIBILITY_MATRIX[format];
    const allowedCodecs = rules ? rules[type] : null;

    if (!allowedCodecs) {
      return true;
    }

    return allowedCodecs.includes(codecValueForCompatibility(codec, type));
  }

  function rebuildCodecSelect(selectId, type, preferredDefault) {
    const select = document.getElementById(selectId);
    const format = document.getElementById('conv-format').value;
    const currentValue = select.value;
    const compatibleOptions = CODEC_OPTION_CATALOG[selectId].filter(option =>
      isCodecOptionCompatible(format, option.value, type)
    );

    select.innerHTML = '';
    if (compatibleOptions.length === 0) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = `No compatible ${type} encoders`;
      select.appendChild(option);
      return;
    }

    compatibleOptions.forEach(optionData => {
      const option = document.createElement('option');
      option.value = optionData.value;
      option.textContent = optionData.text;
      select.appendChild(option);
    });

    if (compatibleOptions.some(option => option.value === currentValue)) {
      select.value = currentValue;
    } else if (compatibleOptions.some(option => option.value === preferredDefault)) {
      select.value = preferredDefault;
    } else if (compatibleOptions.length > 0) {
      select.value = compatibleOptions[0].value;
    }
  }

  function updateCodecOptionsForFormat() {
    rebuildCodecSelect('conv-vcodec', 'video', 'libx264');
    rebuildCodecSelect('conv-acodec', 'audio', 'aac');
  }

  function checkTranscodingCompatibility() {
    const format = document.getElementById('conv-format').value;
    let videoCodec = document.getElementById('conv-vcodec').value;
    let audioCodec = document.getElementById('conv-acodec').value;

    const warningBanner = document.getElementById('conv-compat-warning');
    const warningText = document.getElementById('conv-compat-warning-text');

    if (!warningBanner || !warningText) return;

    if (!videoCodec || !audioCodec) {
      const missingType = !videoCodec && !audioCodec ? 'video or audio encoders' : !videoCodec ? 'video encoders' : 'audio encoders';
      warningText.textContent = `No compatible ${missingType} are available for the selected container. Choose a different target format.`;
      warningBanner.classList.remove('hidden');
      btnStartProcess.disabled = true;
      btnStartProcess.style.opacity = '0.5';
      btnStartProcess.style.cursor = 'not-allowed';
      return;
    }

    videoCodec = codecValueForCompatibility(videoCodec, 'video');
    audioCodec = codecValueForCompatibility(audioCodec, 'audio');

    const rules = COMPATIBILITY_MATRIX[format];
    if (!rules) {
      warningBanner.classList.add('hidden');
      btnStartProcess.disabled = false;
      btnStartProcess.style.opacity = '';
      btnStartProcess.style.cursor = '';
      return;
    }

    let isVideoIncompatible = false;
    let isAudioIncompatible = false;

    if (rules.video && !rules.video.includes(videoCodec)) {
      isVideoIncompatible = true;
    }
    if (rules.audio && !rules.audio.includes(audioCodec)) {
      isAudioIncompatible = true;
    }

    if (isVideoIncompatible || isAudioIncompatible) {
      let msg = "";
      if (isVideoIncompatible && isAudioIncompatible) {
        msg = `Incompatible combination: Video Codec (${videoCodec}) and Audio Codec (${audioCodec}) are incompatible with the ${format.toUpperCase()} container. `;
      } else if (isVideoIncompatible) {
        msg = `Incompatible combination: Video Codec (${videoCodec}) is incompatible with the ${format.toUpperCase()} container. `;
      } else {
        msg = `Incompatible combination: Audio Codec (${audioCodec}) is incompatible with the ${format.toUpperCase()} container. `;
      }
      msg += rules.notes;
      warningText.textContent = msg;
      warningBanner.classList.remove('hidden');
      btnStartProcess.disabled = true;
      btnStartProcess.style.opacity = '0.5';
      btnStartProcess.style.cursor = 'not-allowed';
    } else {
      warningBanner.classList.add('hidden');
      btnStartProcess.disabled = false;
      btnStartProcess.style.opacity = '';
      btnStartProcess.style.cursor = '';
    }
  }

  document.getElementById('conv-format').addEventListener('change', () => {
    updateCodecOptionsForFormat();
    checkTranscodingCompatibility();
  });
  document.getElementById('conv-vcodec').addEventListener('change', checkTranscodingCompatibility);
  document.getElementById('conv-acodec').addEventListener('change', checkTranscodingCompatibility);

  // Frames Mode fields visibility
  framesMode.addEventListener('change', () => {
    const val = framesMode.value;
    if (val === 'single') {
      groupTimestamp.classList.remove('hidden');
      groupDuration.classList.add('hidden');
      groupFps.classList.add('hidden');
    } else if (val === 'interval') {
      groupTimestamp.classList.add('hidden');
      groupDuration.classList.add('hidden');
      groupFps.classList.remove('hidden');
      lblFps.textContent = "Framerate (FPS, e.g. 0.1 for 1 frame every 10s)";
    } else if (val === 'gif') {
      groupTimestamp.classList.remove('hidden');
      groupDuration.classList.remove('hidden');
      groupFps.classList.remove('hidden');
      lblFps.textContent = "Output GIF Frame Rate (FPS)";
    }
  });

  // Merge Mode fields visibility
  mergeType.addEventListener('change', () => {
    const val = mergeType.value;
    if (val === 'audio') {
      groupMergeAudioMode.classList.remove('hidden');
      groupMergeSubMode.classList.add('hidden');
      lblMergeFile.textContent = "External Audio File Absolute Path";
    } else {
      groupMergeAudioMode.classList.add('hidden');
      groupMergeSubMode.classList.remove('hidden');
      lblMergeFile.textContent = "External Subtitle File Absolute Path (.srt, .vtt)";
    }
  });

  extractElementType.addEventListener('change', () => {
    const val = extractElementType.value;
    if (val === 'subtitles') {
      extractSubFormatGroup.classList.remove('hidden');
    } else {
      extractSubFormatGroup.classList.add('hidden');
    }
  });

  // --- 6. Start Processing Job ---
  btnStartProcess.addEventListener('click', async () => {
    if (!sourceFilePath) {
      showError("Execution Blocked", "Please choose and analyze a source media file first.");
      return;
    }

    const payload = {
      input_path: sourceFilePath,
      output_path: customOutputPath.value.trim() || null
    };

    if (activeTab === 'tab-convert') {
      payload.operation = 'convert';
      payload.format = document.getElementById('conv-format').value;
      payload.hw_accel = document.getElementById('conv-hwaccel').value;
      payload.video_codec = document.getElementById('conv-vcodec').value;
      payload.resolution = document.getElementById('conv-resolution').value;
      payload.preset = document.getElementById('conv-preset').value;
      payload.crf = parseInt(convCrf.value);
      
      const vBitrate = document.getElementById('conv-vbitrate').value.trim();
      if (vBitrate) payload.video_bitrate = vBitrate;
      
      payload.audio_codec = document.getElementById('conv-acodec').value;
      payload.audio_bitrate = document.getElementById('conv-abitrate').value;
      
      const chOverride = document.getElementById('audio-channels-select').value;
      if (chOverride) payload.audio_channels = parseInt(chOverride);
      
      const checkedAudio = document.querySelector('input[name="audio-stream-radio"]:checked');
      if (checkedAudio) {
        const relativeIdx = probedData.audio_streams.findIndex(s => s.index == checkedAudio.value);
        if (relativeIdx !== -1) payload.audio_track = relativeIdx;
      }
      
      const checkedSub = document.querySelector('input[name="sub-stream-radio"]:checked');
      if (checkedSub && checkedSub.value !== "-1") {
        const relativeSubIdx = probedData.subtitle_streams.findIndex(s => s.index == checkedSub.value);
        if (relativeSubIdx !== -1) {
          payload.sub_track = relativeSubIdx;
          payload.sub_mode = document.getElementById('conv-sub-mode').value; 
        }
      } else if (checkedSub && checkedSub.value === "-1") {
        payload.sub_track = -1;
      }

    } else if (activeTab === 'tab-audio') {
      payload.operation = 'extract_audio';
      payload.audio_codec = document.getElementById('extract-audio-codec').value;
      payload.audio_bitrate = document.getElementById('extract-audio-bitrate').value;
      
      const checkedAudio = document.querySelector('input[name="audio-stream-radio"]:checked');
      if (checkedAudio) {
        const relativeIdx = probedData.audio_streams.findIndex(s => s.index == checkedAudio.value);
        if (relativeIdx !== -1) payload.audio_track = relativeIdx;
      }

    } else if (activeTab === 'tab-video') {
      payload.operation = 'extract_video';
      payload.video_codec = document.getElementById('extract-video-codec').value;

    } else if (activeTab === 'tab-extract') {
      const type = extractElementType.value;
      if (type === 'subtitles') {
        payload.operation = 'extract_subs';
        payload.sub_format = document.getElementById('extract-sub-format').value;
        const checkedSub = document.querySelector('input[name="sub-stream-radio"]:checked');
        if (checkedSub && checkedSub.value !== "-1") {
          const relativeSubIdx = probedData.subtitle_streams.findIndex(s => s.index == checkedSub.value);
          if (relativeSubIdx !== -1) payload.sub_track = relativeSubIdx;
        } else {
          showError("Muxing Interrupted", "Please choose a subtitle track to extract from the Specs panel.");
          return;
        }
      } else {
        payload.operation = 'extract_chapters';
      }

    } else if (activeTab === 'tab-frames') {
      payload.operation = 'extract_frames';
      payload.image_format = document.getElementById('frames-format').value;
      payload.image_mode = framesMode.value;
      payload.timestamp = document.getElementById('frames-timestamp').value.trim();
      payload.duration = parseFloat(document.getElementById('frames-duration').value);
      payload.interval_fps = parseFloat(document.getElementById('frames-fps').value);

    } else if (activeTab === 'tab-thumbs') {
      payload.operation = 'thumbnail_grid';
      payload.grid_rows = parseInt(document.getElementById('grid-rows').value);
      payload.grid_cols = parseInt(document.getElementById('grid-cols').value);

    } else if (activeTab === 'tab-merge') {
      const mergeM = mergeType.value;
      const mergePath = document.getElementById('merge-filepath').value.trim();
      if (!mergePath) {
        showError("Missing Files", "Please provide a path for the external audio/subtitle file.");
        return;
      }
      
      if (mergeM === 'audio') {
        payload.operation = 'embed_audio';
        payload.audio_file = mergePath;
        payload.embed_mode = document.getElementById('merge-audio-mode').value;
      } else {
        payload.operation = 'embed_subs';
        payload.sub_file = mergePath;
        payload.sub_mode = document.getElementById('merge-sub-mode').value;
      }
    }

    payload.session_id = sessionId;

    if (isBatchMode && batchQueue.length > 0) {
      batchIndex = 0;
      batchResults = [];
      batchStartTime = Date.now();
      startNextBatchItem(payload);
    } else {
      batchQueue = [];
      isBatchMode = false;
      batchStartTime = null;
      startJob(payload);
    }
  });

  async function startNextBatchItem(basePayload) {
    if (batchIndex >= batchQueue.length) {
      processingModal.classList.add('hidden');
      document.body.classList.remove('modal-open');
      document.documentElement.classList.remove('modal-open');
      showCompletion(null);
      return;
    }
    
    currentBasePayload = basePayload;
    const activeFile = batchQueue[batchIndex];
    const itemPayload = {
      ...basePayload,
      input_path: activeFile
    };
    
    startJob(itemPayload);
  }

  async function startJob(payload) {
    stopProcessPreview();
    processingModal.classList.remove('hidden');
    document.body.classList.add('modal-open');
    document.documentElement.classList.add('modal-open');
    
    const filename = payload.input_path.replace(/\\/g, '/').split('/').pop();
    if (isBatchMode) {
      loadingTxtLabel.textContent = `Batch [${batchIndex + 1}/${batchQueue.length}]`;
      document.getElementById('loading-txt-label').style.letterSpacing = '2px';
      consoleOutput.textContent = `[Batch Job] Starting file ${batchIndex + 1} of ${batchQueue.length}: ${filename}...`;
      const batchWrap = document.getElementById('batch-progress-wrap');
      const batchVal = document.getElementById('batch-progress-value');
      if (batchWrap && batchVal) {
        batchWrap.classList.remove('hidden');
        batchVal.textContent = `${batchIndex} of ${batchQueue.length} done`;
      }
    } else {
      loadingTxtLabel.textContent = "Loading";
      document.getElementById('loading-txt-label').style.letterSpacing = '7px';
      consoleOutput.textContent = "Connecting to pipeline service...";
      const batchWrap = document.getElementById('batch-progress-wrap');
      if (batchWrap) {
        batchWrap.classList.add('hidden');
      }
    }
    
    statusPercent.textContent = "0%";
    statusSpeed.textContent = "0.0x";
    statusEta.textContent = "Pending";
    statusSize.textContent = "0 B";
    document.getElementById('processor-value').textContent = 'Initializing...';

    try {
      const response = await fetch('/api/convert', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken
        },
        body: JSON.stringify(payload)
      });
      
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "Failed to trigger process.");
      }
      
      activeJobId = data.job_id;
      pollInterval = setInterval(pollJobStatus, 800);
      
    } catch (err) {
      if (isBatchMode) {
        batchResults.push({
          name: filename,
          error: err.message,
          status: 'failed'
        });
        batchIndex++;
        startNextBatchItem(currentBasePayload);
      } else {
        processingModal.classList.add('hidden');
        document.body.classList.remove('modal-open');
        document.documentElement.classList.remove('modal-open');
        showError("Trigger Failed", err.message);
      }
    }
  }

  // --- 7. Poll Conversion Status ---
  async function pollJobStatus() {
    if (!activeJobId) return;
    
    try {
      const response = await fetch(`/api/status/${activeJobId}`);
      if (!response.ok) {
        throw new Error("Failed to query status.");
      }
      
      const data = await response.json();
      
      loadingTxtLabel.textContent = data.status === 'running' ? 'Processing' : data.status;
      statusPercent.textContent = `${Math.round(data.progress)}%`;
      statusSpeed.textContent = data.speed;
      statusEta.textContent = data.eta;
      statusSize.textContent = data.size;

      if (isBatchMode && batchStartTime) {
        const batchWrap = document.getElementById('batch-progress-wrap');
        const batchVal = document.getElementById('batch-progress-value');
        if (batchWrap && batchVal) {
          batchWrap.classList.remove('hidden');
          const elapsed = (Date.now() - batchStartTime) / 1000;
          const completedCount = batchIndex;
          const currentProgress = data.progress || 0;
          const fractionalCompleted = completedCount + (currentProgress / 100);
          let batchStatusText = `${batchIndex} of ${batchQueue.length} done`;
          if (fractionalCompleted > 0) {
            const avgDuration = elapsed / fractionalCompleted;
            const remainingFiles = batchQueue.length - fractionalCompleted;
            const etaSecs = remainingFiles * avgDuration;
            batchStatusText += `, ${formatRemainingTime(etaSecs)}`;
          } else {
            batchStatusText += `, estimating...`;
          }
          batchVal.textContent = batchStatusText;
        }
      }
      
      // Update the live frame image preview if checked
      updatePreviewFrame();
      
      if (data.encoder) {
        document.getElementById('processor-value').textContent = data.encoder;
      }
      
      if (data.log && data.log.length > 0) {
        consoleOutput.textContent = data.log.join('\n');
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
      }
      
      if (data.status === 'completed') {
        stopPolling();
        stopProcessPreview();
        if (isBatchMode) {
          batchResults.push({
            name: batchQueue[batchIndex].replace(/\\/g, '/').split('/').pop(),
            path: data.output_path,
            size: data.size,
            status: 'completed'
          });
          batchIndex++;
          startNextBatchItem(currentBasePayload);
        } else {
          processingModal.classList.add('hidden');
          document.body.classList.remove('modal-open');
          document.documentElement.classList.remove('modal-open');
          showCompletion(data);
        }
      } else if (data.status === 'failed') {
        stopPolling();
        stopProcessPreview();
        if (isBatchMode) {
          batchResults.push({
            name: batchQueue[batchIndex].replace(/\\/g, '/').split('/').pop(),
            error: data.error || "FFmpeg encountered an unrecoverable failure.",
            status: 'failed'
          });
          batchIndex++;
          startNextBatchItem(currentBasePayload);
        } else {
          processingModal.classList.add('hidden');
          document.body.classList.remove('modal-open');
          document.documentElement.classList.remove('modal-open');
          showError("Operation Failed", data.error || "FFmpeg encountered an unrecoverable failure.");
        }
      } else if (data.status === 'cancelled') {
        stopPolling();
        stopProcessPreview();
        isBatchMode = false;
        processingModal.classList.add('hidden');
        document.body.classList.remove('modal-open');
        document.documentElement.classList.remove('modal-open');
      }
      
    } catch (err) {
      console.error(err);
    }
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
    activeJobId = null;
  }

  // --- 8. Cancellation ---
  btnCancelProcess.addEventListener('click', async () => {
    if (activeJobId) {
      btnCancelProcess.disabled = true;
      btnCancelProcess.textContent = "Stopping...";
      
      try {
        await fetch(`/api/cancel/${activeJobId}`, {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrfToken }
        });
      } catch (err) {
        console.error("Cancellation request failed:", err);
      } finally {
        btnCancelProcess.disabled = false;
        btnCancelProcess.textContent = "ABORT PIPELINE";
      }
    }
    
    stopPolling();
    stopProcessPreview();
    isBatchMode = false; // Cancel remaining batch conversions
    processingModal.classList.add('hidden');
    document.body.classList.remove('modal-open');
    document.documentElement.classList.remove('modal-open');
  });

  // --- 9. Completion Actions ---
  function showCompletion(jobData) {
    completionModal.classList.remove('hidden');
    
    const summaryCard = document.getElementById('complete-summary-card');
    
    if (jobData) {
      // Single file completion
      summaryCard.innerHTML = `
        <div class="summary-line">
          <span class="label">Output File</span>
          <span class="value font-mono" id="complete-path">${escapeHtml(jobData.output_path)}</span>
        </div>
        <div class="summary-line">
          <span class="label">Final File Size</span>
          <span class="value" id="complete-size">${escapeHtml(jobData.size)}</span>
        </div>
      `;
      
      if (isLocalMode) {
        btnOpenFolder.classList.remove('hidden');
        btnDownloadFile.classList.add('hidden');
        
        btnOpenFolder.onclick = async () => {
          try {
            await fetch('/api/open-folder', {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
              },
              body: JSON.stringify({ filepath: jobData.output_path })
            });
          } catch (e) {
            console.error(e);
          }
        };
      } else {
        btnOpenFolder.classList.add('hidden');
        btnDownloadFile.classList.remove('hidden');
        
        const filename = jobData.output_path.replace(/\\/g, '/').split('/').pop();
        btnDownloadFile.href = `/api/download/${sessionId}/${encodeURIComponent(filename)}`;
      }
    } else {
      // Batch completion
      const firstSuccess = batchResults.find(r => r.status === 'completed');
      
      if (isLocalMode) {
        summaryCard.innerHTML = batchResults.map((res, idx) => `
          <div class="summary-line" style="border-bottom: 1px dashed rgba(255, 255, 255, 0.05); padding-bottom: 10px; margin-bottom: 10px;">
            <span class="label">File #${idx + 1}: ${escapeHtml(res.name)}</span>
            <span class="value font-mono" style="${res.status === 'failed' ? 'color: #ef4444;' : 'color: var(--neon-cyan);'}">${res.status === 'completed' ? escapeHtml(res.path) : 'Failed: ' + escapeHtml(res.error)}</span>
            ${res.status === 'completed' ? `<span class="value" style="font-size: 10px; color: var(--text-dim); margin-top: 2px;">Size: ${escapeHtml(res.size)}</span>` : ''}
          </div>
        `).join('');
        
        if (firstSuccess) {
          btnOpenFolder.classList.remove('hidden');
          btnDownloadFile.classList.add('hidden');
          
          btnOpenFolder.onclick = async () => {
            try {
              await fetch('/api/open-folder', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ filepath: firstSuccess.path })
              });
            } catch (e) {
              console.error(e);
            }
          };
        } else {
          btnOpenFolder.classList.add('hidden');
          btnDownloadFile.classList.add('hidden');
        }
      } else {
        // Web uploads batch: provide individual download links inside the card
        summaryCard.innerHTML = batchResults.map((res, idx) => `
          <div class="summary-line" style="border-bottom: 1px dashed rgba(255, 255, 255, 0.05); padding-bottom: 10px; margin-bottom: 10px;">
            <span class="label">File #${idx + 1}: ${escapeHtml(res.name)}</span>
            <span class="value font-mono" style="${res.status === 'failed' ? 'color: #ef4444;' : 'color: var(--neon-cyan);'}">${res.status === 'completed' ? escapeHtml(res.path) : 'Failed: ' + escapeHtml(res.error)}</span>
            ${res.status === 'completed' ? `
              <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 4px;">
                <span style="font-size: 10px; color: var(--text-dim);">Size: ${escapeHtml(res.size)}</span>
                <a href="/api/download/${sessionId}/${encodeURIComponent(res.path.replace(/\\/g, '/').split('/').pop())}" download style="font-size: 11px; color: var(--neon-cyan); text-decoration: underline;">Download File</a>
              </div>
            ` : ''}
          </div>
        `).join('');
        
        btnOpenFolder.classList.add('hidden');
        btnDownloadFile.classList.add('hidden');
      }
    }
  }

  btnCompleteClose.addEventListener('click', () => {
    completionModal.classList.add('hidden');
  });

  // --- 10. Error Modal ---
  function showError(title, msg) {
    errorModal.classList.remove('hidden');
    errorDetailsBox.textContent = `${title}\n\n${msg}`;
  }

  btnErrorClose.addEventListener('click', () => {
    errorModal.classList.add('hidden');
  });

  // --- 11. Load Hardware Encoders ---
  async function loadHwEncoders() {
    try {
      const response = await fetch('/api/hw-encoders');
      if (!response.ok) return;
      const data = await response.json();
      const select = document.getElementById('conv-hwaccel');
      if (!select) return;
      
      const encoderNames = {
        'nvenc': 'NVIDIA NVENC (Fastest)',
        'qsv': 'Intel Quick Sync Video (QSV)',
        'amf': 'AMD Advanced Media Framework (AMF)',
        'mf': 'Windows Media Foundation (MF)'
      };
      
      // Remove any existing dynamic options to prevent duplicates
      for (let i = select.options.length - 1; i >= 0; i--) {
        const val = select.options[i].value;
        if (val !== 'auto' && val !== 'none') {
          select.remove(i);
        }
      }
      
      data.supported.forEach(hw => {
        const option = document.createElement('option');
        option.value = hw;
        option.textContent = encoderNames[hw] || hw.toUpperCase();
        select.insertBefore(option, select.options[1]);
      });

      // Automatically switch to the best detected hardware encoder
      if (data.supported.includes('nvenc')) {
        select.value = 'nvenc';
      } else if (data.supported.length > 0) {
        const order = ['qsv', 'amf', 'mf'];
        let matched = false;
        for (const candidate of order) {
          if (data.supported.includes(candidate)) {
            select.value = candidate;
            matched = true;
            break;
          }
        }
        if (!matched) {
          select.value = data.supported[0];
        }
      }

      // Display physical GPUs detected
      const gpusContainer = document.getElementById('detected-gpus');
      if (gpusContainer && data.gpus && data.gpus.length > 0) {
        gpusContainer.innerHTML = '';
        data.gpus.forEach(gpu => {
          const gpuDiv = document.createElement('div');
          gpuDiv.className = 'gpu-badge';
          gpuDiv.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
              <rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect>
              <line x1="6" y1="6" x2="6.01" y2="6"></line>
              <line x1="6" y1="18" x2="6.01" y2="18"></line>
            </svg>
            <span>${escapeHtml(gpu)}</span>
          `;
          gpusContainer.appendChild(gpuDiv);
        });
      }
    } catch (e) {
      console.error("Failed to load hardware encoders:", e);
    }
  }

  // --- 12. Processing Live Video Preview Toggle Events ---
  const chkProcessPreview = document.getElementById('chk-process-preview');
  const processPreviewImg = document.getElementById('process-preview-img');
  const processingLayout = document.getElementById('processing-layout');

  if (chkProcessPreview) {
    chkProcessPreview.addEventListener('change', () => {
      if (chkProcessPreview.checked) {
        processingLayout.classList.add('show-preview');
        updatePreviewFrame();
      } else {
        processingLayout.classList.remove('show-preview');
        if (processPreviewImg) {
          processPreviewImg.removeAttribute('src');
        }
      }
    });
  }

  let isPreviewLoading = false;

  function updatePreviewFrame() {
    if (chkProcessPreview && chkProcessPreview.checked && activeJobId && processPreviewImg) {
      if (isPreviewLoading) return;
      isPreviewLoading = true;
      
      processPreviewImg.onload = () => {
        isPreviewLoading = false;
      };
      processPreviewImg.onerror = () => {
        isPreviewLoading = false;
      };
      
      processPreviewImg.src = `/api/job-frame/${activeJobId}?t=${Date.now()}`;
    }
  }

  function stopProcessPreview() {
    isPreviewLoading = false;
    if (chkProcessPreview) {
      chkProcessPreview.checked = false;
    }
    if (processingLayout) {
      processingLayout.classList.remove('show-preview');
    }
    if (processPreviewImg) {
      processPreviewImg.removeAttribute('src');
    }
  }

  // --- 13. Session Cleanup on Tab/Window Unload ---
  let isDownloading = false;
  document.addEventListener('click', (e) => {
    const target = e.target.closest('a');
    if (target && target.href && target.href.includes('/api/download/')) {
      isDownloading = true;
      setTimeout(() => {
        isDownloading = false;
      }, 2000);
    }
  });

  window.addEventListener('beforeunload', () => {
    if (isDownloading) return;
    navigator.sendBeacon(`/api/cleanup-session?session_id=${sessionId}&csrf_token=${encodeURIComponent(csrfToken)}`);
  });

  // --- 14. Engine Quit / Termination Endpoint ---
  const btnQuit = document.getElementById('btn-quit');
  if (btnQuit) {
    btnQuit.addEventListener('click', async () => {
      if (confirm("Are you sure you want to shut down Obsidian Codec Engine? This will terminate the terminal process and close the page.")) {
        const shutdownOverlay = document.getElementById('shutdown-overlay');
        if (shutdownOverlay) {
          shutdownOverlay.classList.remove('hidden');
        }
        
        try {
          await fetch('/api/quit', {
            method: 'POST',
            headers: { 'X-CSRF-Token': csrfToken }
          });
        } catch (e) {
          console.error("Shutdown call error:", e);
        }
        
        setTimeout(() => {
          window.close();
        }, 1000);
      }
    });
  }

  (async () => {
    await fetchCsrfToken();
    loadHwEncoders();
  })();
});
