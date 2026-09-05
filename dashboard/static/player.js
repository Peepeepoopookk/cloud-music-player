/**
 * Wavify Web Player - Spotify-Style Playback Engine
 */
document.addEventListener('DOMContentLoaded', () => {
    // State Machine
    const state = {
        allTracks: [],
        allArtists: [],
        allPlaylists: [],
        currentTrack: null,
        currentContext: { type: 'library', name: 'All Tracks', tracks: [] },
        queue: [],
        queueIndex: 0,
        isPlaying: false,
        shuffle: false,
        repeat: 'off', // 'off' | 'all' | 'one'
        volume: 1.0,
        isMuted: false,
        unmutedVolume: 1.0,
        activeTab: 'home',
        recentHistory: []
    };

    // Restore volume from localStorage
    try {
        const savedVol = localStorage.getItem('wavify_web_player_volume');
        if (savedVol !== null) {
            state.volume = parseFloat(savedVol) || 1.0;
        }
    } catch (e) {}

    // DOM Elements
    const audio = document.getElementById('audio-player');
    const pbArtwork = document.getElementById('pb-artwork');
    const pbTitle = document.getElementById('pb-title');
    const pbArtist = document.getElementById('pb-artist');
    const btnPlayPause = document.getElementById('btn-play-pause');
    const btnPrev = document.getElementById('btn-prev');
    const btnNext = document.getElementById('btn-next');
    const btnShuffle = document.getElementById('btn-shuffle');
    const btnRepeat = document.getElementById('btn-repeat');
    const seekSlider = document.getElementById('seek-slider');
    const timeCurrent = document.getElementById('time-current');
    const timeDuration = document.getElementById('time-duration');
    const volumeSlider = document.getElementById('volume-slider');
    const volumeIcon = document.getElementById('volume-icon');
    const btnToggleQueue = document.getElementById('btn-toggle-queue');
    const queueDrawer = document.getElementById('queue-drawer');
    const queueList = document.getElementById('queue-list');
    const queueBadge = document.getElementById('queue-badge');
    const queueContextLabel = document.getElementById('queue-context-label');
    const btnCloseQueue = document.getElementById('btn-close-queue');
    const btnClearQueue = document.getElementById('btn-clear-queue');

    // Now Playing Side Panel
    const nowPlayingPanel = document.getElementById('now-playing-panel');
    const btnToggleNP = document.getElementById('btn-toggle-np');
    const btnCloseNP = document.getElementById('btn-close-np');
    const npArtworkLarge = document.getElementById('np-artwork-large');
    const npTitle = document.getElementById('np-title');
    const npArtist = document.getElementById('np-artist');
    const npAlbum = document.getElementById('np-album');
    const npExtraTags = document.getElementById('np-extra-tags');

    // Tab views
    const tabBtns = document.querySelectorAll('.player-tab-btn');
    const viewHome = document.getElementById('view-home');
    const viewTracks = document.getElementById('view-tracks');
    const viewArtists = document.getElementById('view-artists');
    const viewPlaylists = document.getElementById('view-playlists');
    const tracksTbody = document.getElementById('player-tracks-tbody');
    const searchInput = document.getElementById('player-search-input');
    const artistsGrid = document.getElementById('player-artists-grid');
    const playlistsGrid = document.getElementById('player-playlists-grid');

    // Home Shelves
    const shelfRecentlyPlayedWrap = document.getElementById('shelf-recently-played-wrap');
    const shelfRecentlyPlayed = document.getElementById('shelf-recently-played');
    const shelfRecentlyAdded = document.getElementById('shelf-recently-added');
    const shelfTopArtists = document.getElementById('shelf-top-artists');
    const shelfFeaturedPlaylists = document.getElementById('shelf-featured-playlists');
    const seeAllRecent = document.getElementById('see-all-recent');
    const seeAllArtists = document.getElementById('see-all-artists');
    const seeAllPlaylists = document.getElementById('see-all-playlists');

    // Context / Subviews
    const artistDetailView = document.getElementById('player-artist-detail');
    const btnBackArtists = document.getElementById('btn-back-artists');
    const artistDetailName = document.getElementById('artist-detail-name');
    const artistDetailCount = document.getElementById('artist-detail-count');
    const artistDetailImage = document.getElementById('artist-detail-image');
    const artistDetailTracks = document.getElementById('artist-detail-tracks');
    const btnPlayArtist = document.getElementById('btn-play-artist');
    const btnShuffleArtist = document.getElementById('btn-shuffle-artist');

    const playlistDetailView = document.getElementById('player-playlist-detail');
    const btnBackPlaylists = document.getElementById('btn-back-playlists');
    const playlistDetailName = document.getElementById('playlist-detail-name');
    const playlistDetailCount = document.getElementById('playlist-detail-count');
    const playlistDetailArt = document.getElementById('playlist-detail-art');
    const playlistDetailTracks = document.getElementById('playlist-detail-tracks');
    const btnPlayPlaylist = document.getElementById('btn-play-playlist');
    const btnShufflePlaylist = document.getElementById('btn-shuffle-playlist');

    // Context Menu & Toast & NP Actions
    const trackContextMenu = document.getElementById('track-context-menu');
    const playerToast = document.getElementById('player-toast');
    const btnNpPlayNext = document.getElementById('btn-np-play-next');
    const btnNpAddQueue = document.getElementById('btn-np-add-queue');
    let pendingSeekTime = null;
    let toastTimeout = null;
    let activeMenuTrack = null;

    // Set initial audio volume
    audio.volume = state.volume;
    if (volumeSlider) volumeSlider.value = state.volume * 100;

    // ==========================================
    // UTILITY FUNCTIONS
    // ==========================================
    function escapeHTML(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function formatTime(seconds) {
        if (!seconds || isNaN(seconds) || seconds < 0) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
    }

    function parseDuration(durationStr) {
        if (!durationStr || durationStr === '--:--') return 0;
        const parts = durationStr.split(':').map(Number);
        if (parts.length === 2) {
            return parts[0] * 60 + parts[1];
        } else if (parts.length === 3) {
            return parts[0] * 3600 + parts[1] * 60 + parts[2];
        }
        return 0;
    }

    function shuffleArray(arr) {
        const copy = [...arr];
        for (let i = copy.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [copy[i], copy[j]] = [copy[j], copy[i]];
        }
        return copy;
    }

    // Smart Shuffle: prioritizes tracks not in the 25-track recentHistory ring buffer
    function shuffleArraySmart(arr) {
        if (!arr || arr.length <= 1) return [...arr];
        const recentIds = new Set(state.recentHistory.map(t => String(t.driveFileId || t.file_id || t.id || '')));
        const fresh = arr.filter(t => !recentIds.has(String(t.driveFileId || t.file_id || t.id || '')));
        const recent = arr.filter(t => recentIds.has(String(t.driveFileId || t.file_id || t.id || '')));

        if (fresh.length > 0) {
            return shuffleArray(fresh).concat(shuffleArray(recent));
        }
        return shuffleArray(arr);
    }

    // 25-Track Ring Buffer
    function addToRecentHistory(track) {
        if (!track) return;
        const trackId = String(track.driveFileId || track.file_id || track.id || '');
        if (!trackId) return;

        state.recentHistory = state.recentHistory.filter(t => {
            const tid = String(t.driveFileId || t.file_id || t.id || '');
            return tid !== trackId;
        });
        state.recentHistory.unshift(track);
        if (state.recentHistory.length > 25) {
            state.recentHistory = state.recentHistory.slice(0, 25);
        }
    }

    function showToast(message) {
        if (!playerToast) return;
        playerToast.textContent = message;
        playerToast.classList.add('show');
        if (toastTimeout) clearTimeout(toastTimeout);
        toastTimeout = setTimeout(() => {
            playerToast.classList.remove('show');
        }, 2200);
    }

    // Queue Actions: Play Next & Add to Queue
    function playNextTrack(track) {
        if (!track) return;
        if (!state.currentTrack || state.queue.length === 0) {
            state.queue = [track];
            state.queueIndex = 0;
            playTrackAt(0);
        } else {
            const insertIndex = state.queueIndex + 1;
            state.queue.splice(insertIndex, 0, track);
            renderQueue();
            saveSessionState();
        }
        showToast(`Playing next: ${track.title || 'Track'}`);
    }

    function addToQueue(track) {
        if (!track) return;
        if (!state.currentTrack || state.queue.length === 0) {
            state.queue = [track];
            state.queueIndex = 0;
            playTrackAt(0);
        } else {
            state.queue.push(track);
            renderQueue();
            saveSessionState();
        }
        showToast(`Added to queue: ${track.title || 'Track'}`);
    }

    // Session Persistence
    function sanitizeTrackForStorage(t) {
        if (!t) return null;
        return {
            id: t.id || t.driveFileId || t.file_id,
            driveFileId: t.driveFileId || t.file_id || t.id,
            title: t.title || '',
            artist: t.artist || '',
            album: t.album || '',
            album_art: t.album_art || t.albumArt || '',
            duration: t.duration || '--:--'
        };
    }

    function saveSessionState() {
        try {
            const session = {
                queue: state.queue.map(sanitizeTrackForStorage),
                queueIndex: state.queueIndex,
                currentTrack: sanitizeTrackForStorage(state.currentTrack),
                currentTime: audio.currentTime || 0,
                shuffle: state.shuffle,
                repeat: state.repeat,
                currentContext: {
                    type: state.currentContext.type || 'library',
                    name: state.currentContext.name || 'All Tracks'
                },
                recentHistory: state.recentHistory.map(sanitizeTrackForStorage)
            };
            sessionStorage.setItem('wavify_player_session', JSON.stringify(session));
        } catch (e) {
            console.warn("Failed to save player session:", e);
        }
    }

    function restoreSessionState() {
        try {
            const raw = sessionStorage.getItem('wavify_player_session');
            if (!raw) return;
            const session = JSON.parse(raw);

            if (session.recentHistory && Array.isArray(session.recentHistory)) {
                state.recentHistory = session.recentHistory;
            }
            if (session.shuffle !== undefined) {
                state.shuffle = !!session.shuffle;
                btnShuffle.classList.toggle('active', state.shuffle);
                btnShuffle.title = state.shuffle ? "Shuffle: On" : "Shuffle: Off";
            }
            if (session.repeat) {
                state.repeat = session.repeat;
                btnRepeat.classList.toggle('active', state.repeat !== 'off');
                btnRepeat.classList.toggle('repeat-one', state.repeat === 'one');
                btnRepeat.title = `Repeat: ${state.repeat.charAt(0).toUpperCase() + state.repeat.slice(1)}`;
            }
            if (session.currentContext) {
                state.currentContext = {
                    type: session.currentContext.type || 'library',
                    name: session.currentContext.name || 'All Tracks',
                    tracks: []
                };
            }
            if (session.queue && Array.isArray(session.queue) && session.queue.length > 0) {
                state.queue = session.queue;
                state.queueIndex = typeof session.queueIndex === 'number' ? session.queueIndex : 0;
                renderQueue();
            }
            if (session.currentTrack) {
                state.currentTrack = session.currentTrack;
                const fileId = state.currentTrack.driveFileId || state.currentTrack.file_id || state.currentTrack.id;
                if (fileId) {
                    audio.src = `/stream/${encodeURIComponent(fileId)}`;
                }
                const savedTime = parseFloat(session.currentTime) || 0;
                if (savedTime > 0) {
                    pendingSeekTime = savedTime;
                    audio.currentTime = savedTime;
                    timeCurrent.textContent = formatTime(savedTime);
                    const durSec = parseDuration(state.currentTrack.duration);
                    if (durSec > 0) {
                        seekSlider.value = (savedTime / durSec) * 100;
                    }
                }
                updatePlaybackBarUI();
                state.isPlaying = false;
                updatePlayPauseUI();
            }
        } catch (e) {
            console.warn("Failed to restore player session:", e);
        }
    }

    // Context Menu Positioning & Interaction
    function openContextMenu(e, track) {
        if (!trackContextMenu || !track) return;
        e.preventDefault();
        e.stopPropagation();

        activeMenuTrack = track;
        trackContextMenu.style.display = 'flex';

        const menuWidth = 175;
        const menuHeight = 85;

        let x, y;
        const triggerBtn = e.target && e.target.closest ? e.target.closest('.track-menu-btn') : null;
        if (triggerBtn) {
            const rect = triggerBtn.getBoundingClientRect();
            x = rect.left - menuWidth + rect.width;
            y = rect.bottom + 4;
        } else {
            x = e.clientX || 100;
            y = e.clientY || 100;
        }

        if (x + menuWidth > window.innerWidth - 8) {
            x = window.innerWidth - menuWidth - 8;
        }
        if (y + menuHeight > window.innerHeight - 8) {
            y = window.innerHeight - menuHeight - 8;
        }
        if (x < 8) x = 8;
        if (y < 8) y = 8;

        trackContextMenu.style.left = `${x}px`;
        trackContextMenu.style.top = `${y}px`;
    }

    function closeContextMenu() {
        if (trackContextMenu) {
            trackContextMenu.style.display = 'none';
        }
        activeMenuTrack = null;
    }

    function getThumbnailUrl(url) {
        if (!url) return '';
        if (url.includes('mzstatic.com')) {
            return url.replace('600x600bb', '100x100bb').replace('600x600', '100x100');
        }
        return url;
    }

    // ==========================================
    // PLAYBACK CONTROLLER
    // ==========================================
    function playTrackAt(index) {
        if (index < 0 || index >= state.queue.length) return;
        state.queueIndex = index;
        const track = state.queue[index];
        state.currentTrack = track;
        addToRecentHistory(track);
        renderRecentlyPlayedShelf();

        const fileId = track.driveFileId || track.file_id || track.id;
        const streamUrl = `/stream/${encodeURIComponent(fileId)}`;

        audio.src = streamUrl;
        audio.load();
        audio.play().then(() => {
            state.isPlaying = true;
            updatePlayPauseUI();
        }).catch(err => {
            console.warn("Auto-play blocked or streaming error:", err);
            state.isPlaying = false;
            updatePlayPauseUI();
        });

        updatePlaybackBarUI();
        updateMediaSession(track);
        updateActiveTrackRowHighlight();
        renderQueue();
        saveSessionState();
    }

    function play() {
        if (!state.currentTrack && state.queue.length > 0) {
            playTrackAt(0);
            return;
        }
        if (state.currentTrack) {
            audio.play().then(() => {
                state.isPlaying = true;
                updatePlayPauseUI();
            }).catch(console.warn);
        }
    }

    function pause() {
        audio.pause();
        state.isPlaying = false;
        updatePlayPauseUI();
    }

    function togglePlayPause() {
        if (state.isPlaying) {
            pause();
        } else {
            play();
        }
    }

    function playNext() {
        if (state.repeat === 'one') {
            audio.currentTime = 0;
            audio.play();
            return;
        }

        if (state.queueIndex < state.queue.length - 1) {
            playTrackAt(state.queueIndex + 1);
        } else {
            // Reached end of current queue
            if (state.repeat === 'all') {
                playTrackAt(0);
            } else {
                // Endless shuffle fallthrough: Grab whole library (excluding current track), shuffle with smart ring-buffer deprioritization, and append
                const currentId = state.currentTrack ? String(state.currentTrack.driveFileId || state.currentTrack.id || state.currentTrack.file_id) : '';
                const pool = state.allTracks.filter(t => String(t.driveFileId || t.id || t.file_id) !== currentId);
                if (pool.length > 0) {
                    const shuffledPool = shuffleArraySmart(pool);
                    state.queue = state.queue.concat(shuffledPool);
                    saveSessionState();
                    playTrackAt(state.queueIndex + 1);
                } else {
                    pause();
                }
            }
        }
    }

    function playPrevious() {
        if (audio.currentTime > 3) {
            audio.currentTime = 0;
            return;
        }
        if (state.queueIndex > 0) {
            playTrackAt(state.queueIndex - 1);
        } else {
            audio.currentTime = 0;
        }
    }

    function toggleShuffle() {
        state.shuffle = !state.shuffle;
        btnShuffle.classList.toggle('active', state.shuffle);
        btnShuffle.title = state.shuffle ? "Shuffle: On" : "Shuffle: Off";

        if (state.currentTrack && state.currentContext.tracks.length > 1) {
            const current = state.currentTrack;
            const currentId = String(current.driveFileId || current.id || current.file_id);
            const rest = state.currentContext.tracks.filter(t => String(t.driveFileId || t.id || t.file_id) !== currentId);

            if (state.shuffle) {
                state.queue = [current].concat(shuffleArraySmart(rest));
                state.queueIndex = 0;
            } else {
                // Restore natural context order starting at current track
                const naturalIdx = state.currentContext.tracks.findIndex(t => String(t.driveFileId || t.id || t.file_id) === currentId);
                if (naturalIdx >= 0) {
                    state.queue = [...state.currentContext.tracks];
                    state.queueIndex = naturalIdx;
                }
            }
            renderQueue();
            saveSessionState();
        } else {
            saveSessionState();
        }
    }

    function toggleRepeat() {
        if (state.repeat === 'off') {
            state.repeat = 'all';
            btnRepeat.classList.add('active');
            btnRepeat.classList.remove('repeat-one');
            btnRepeat.title = "Repeat: All";
        } else if (state.repeat === 'all') {
            state.repeat = 'one';
            btnRepeat.classList.add('active', 'repeat-one');
            btnRepeat.title = "Repeat: One";
        } else {
            state.repeat = 'off';
            btnRepeat.classList.remove('active', 'repeat-one');
            btnRepeat.title = "Repeat: Off";
        }
        saveSessionState();
    }

    function startPlaybackContext(contextType, contextName, tracks, startIndex = 0) {
        if (!tracks || tracks.length === 0) return;
        state.currentContext = { type: contextType, name: contextName, tracks: [...tracks] };

        const targetTrack = tracks[startIndex] || tracks[0];
        const targetId = String(targetTrack.driveFileId || targetTrack.id || targetTrack.file_id);
        const rest = tracks.filter(t => String(t.driveFileId || t.id || t.file_id) !== targetId);

        if (state.shuffle) {
            state.queue = [targetTrack].concat(shuffleArraySmart(rest));
            state.queueIndex = 0;
        } else {
            state.queue = [...tracks];
            state.queueIndex = startIndex;
        }

        playTrackAt(state.queueIndex);
    }

    // ==========================================
    // UI UPDATES
    // ==========================================
    function updatePlayPauseUI() {
        const playIcon = `<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>`;
        const pauseIcon = `<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><rect x="5" y="4" width="4" height="16" rx="1"></rect><rect x="15" y="4" width="4" height="16" rx="1"></rect></svg>`;
        btnPlayPause.innerHTML = state.isPlaying ? pauseIcon : playIcon;
        btnPlayPause.title = state.isPlaying ? "Pause" : "Play";

        if ('mediaSession' in navigator) {
            navigator.mediaSession.playbackState = state.isPlaying ? 'playing' : 'paused';
        }
    }

    function updatePlaybackBarUI() {
        const track = state.currentTrack;
        if (!track) return;

        pbTitle.textContent = track.title || 'Unknown Title';
        pbArtist.textContent = track.artist || 'Unknown Artist';

        const albumArtUrl = track.album_art || track.albumArt;
        const defaultArt = "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='54' height='54' viewBox='0 0 24 24' fill='none' stroke='%238a8377' stroke-width='1.5'%3E%3Crect x='3' y='3' width='18' height='18' rx='2'/%3E%3Ccircle cx='12' cy='12' r='4'/%3E%3C/svg%3E";
        const defaultLargeArt = "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='300' viewBox='0 0 24 24' fill='none' stroke='%238a8377' stroke-width='1.5'%3E%3Crect x='3' y='3' width='18' height='18' rx='2'/%3E%3Ccircle cx='12' cy='12' r='4'/%3E%3C/svg%3E";

        if (albumArtUrl) {
            pbArtwork.src = albumArtUrl;
            if (npArtworkLarge) npArtworkLarge.src = albumArtUrl;
        } else {
            pbArtwork.src = defaultArt;
            if (npArtworkLarge) npArtworkLarge.src = defaultLargeArt;
        }

        timeDuration.textContent = track.duration || '--:--';

        // Update Now Playing side panel
        if (npTitle) npTitle.textContent = track.title || 'Unknown Title';
        if (npArtist) {
            npArtist.textContent = track.artist || 'Unknown Artist';
            npArtist.onclick = () => {
                if (track.artist) {
                    openArtistDetail(track.artist);
                }
            };
        }
        if (npAlbum) {
            npAlbum.textContent = track.album ? `Album: ${track.album}` : '';
        }
        if (npExtraTags) {
            let tagsHtml = '';
            if (track.duration) tagsHtml += `<span class="np-tag-chip">⏱ ${escapeHTML(track.duration)}</span>`;
            if (track.year) tagsHtml += `<span class="np-tag-chip">📅 ${escapeHTML(String(track.year))}</span>`;
            if (track.genre) tagsHtml += `<span class="np-tag-chip">🏷 ${escapeHTML(track.genre)}</span>`;
            npExtraTags.innerHTML = tagsHtml;
        }
    }

    function updateActiveTrackRowHighlight() {
        const currentId = state.currentTrack ? (state.currentTrack.driveFileId || state.currentTrack.id) : null;
        document.querySelectorAll('.player-track-row').forEach(row => {
            const rowId = row.getAttribute('data-id');
            const isCurrent = rowId === currentId;
            row.classList.toggle('playing', isCurrent);

            const indexCol = row.querySelector('.track-num-cell');
            if (indexCol) {
                if (isCurrent && state.isPlaying) {
                    indexCol.innerHTML = `<div class="sound-bars"><span></span><span></span><span></span></div>`;
                } else {
                    indexCol.textContent = row.getAttribute('data-index');
                }
            }
        });
    }

    function renderQueue() {
        if (!queueList) return;
        queueList.innerHTML = '';
        queueBadge.textContent = state.queue.length;
        queueContextLabel.textContent = `Context: ${state.currentContext.name || 'Library'}`;

        state.queue.forEach((track, idx) => {
            const isCurrent = idx === state.queueIndex;
            const li = document.createElement('li');
            li.className = `queue-item ${isCurrent ? 'active' : ''}`;
            li.onclick = () => playTrackAt(idx);

            const artUrl = track.album_art || track.albumArt || "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='40' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='1'%3E%3Ccircle cx='12' cy='12' r='4'/%3E%3C/svg%3E";

            li.innerHTML = `
                <img class="queue-item-art" src="${escapeHTML(getThumbnailUrl(artUrl))}" alt="" loading="lazy" decoding="async">
                <div class="queue-item-info">
                    <div class="queue-item-title">${escapeHTML(track.title || 'Unknown Title')}</div>
                    <div class="queue-item-artist">${escapeHTML(track.artist || 'Unknown Artist')}</div>
                </div>
                <div class="queue-item-duration">${track.duration || '--:--'}</div>
            `;
            queueList.appendChild(li);
        });
    }

    // ==========================================
    // WEB MEDIA SESSION API INTEGRATION
    // ==========================================
    function updateMediaSession(track) {
        if (!('mediaSession' in navigator) || !track) return;

        const artUrl = track.album_art || track.albumArt || '';
        const artworkArray = artUrl ? [
            { src: artUrl, sizes: '512x512', type: 'image/jpeg' },
            { src: artUrl, sizes: '256x256', type: 'image/jpeg' }
        ] : [];

        navigator.mediaSession.metadata = new MediaMetadata({
            title: track.title || 'Unknown Title',
            artist: track.artist || 'Unknown Artist',
            album: track.album || 'Cloud Music Player',
            artwork: artworkArray
        });

        // Register Action Handlers
        try {
            navigator.mediaSession.setActionHandler('play', play);
            navigator.mediaSession.setActionHandler('pause', pause);
            navigator.mediaSession.setActionHandler('previoustrack', playPrevious);
            navigator.mediaSession.setActionHandler('nexttrack', playNext);
            navigator.mediaSession.setActionHandler('seekto', (details) => {
                if (details.seekTime !== undefined && details.seekTime !== null) {
                    audio.currentTime = details.seekTime;
                }
            });
            navigator.mediaSession.setActionHandler('seekbackward', (details) => {
                audio.currentTime = Math.max(0, audio.currentTime - (details.seekOffset || 10));
            });
            navigator.mediaSession.setActionHandler('seekforward', (details) => {
                audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + (details.seekOffset || 10));
            });
        } catch (e) {
            console.warn("MediaSession action handler error:", e);
        }
    }

    // ==========================================
    // AUDIO EVENT LISTENERS
    // ==========================================
    let lastTimeSave = 0;
    audio.addEventListener('timeupdate', () => {
        if (!audio.duration || isNaN(audio.duration)) return;
        const current = audio.currentTime;
        const total = audio.duration;
        timeCurrent.textContent = formatTime(current);
        seekSlider.value = (current / total) * 100;

        if ('mediaSession' in navigator && 'setPositionState' in navigator.mediaSession) {
            try {
                navigator.mediaSession.setPositionState({
                    duration: total,
                    playbackRate: audio.playbackRate || 1.0,
                    position: current
                });
            } catch (e) {}
        }

        const now = Date.now();
        if (now - lastTimeSave > 2500) {
            lastTimeSave = now;
            saveSessionState();
        }
    });

    audio.addEventListener('loadedmetadata', () => {
        timeDuration.textContent = formatTime(audio.duration);
        if (pendingSeekTime !== null) {
            audio.currentTime = pendingSeekTime;
            timeCurrent.textContent = formatTime(pendingSeekTime);
            if (audio.duration) {
                seekSlider.value = (pendingSeekTime / audio.duration) * 100;
            }
            pendingSeekTime = null;
        }
    });

    audio.addEventListener('play', () => {
        state.isPlaying = true;
        updatePlayPauseUI();
        updateActiveTrackRowHighlight();
    });

    audio.addEventListener('pause', () => {
        state.isPlaying = false;
        updatePlayPauseUI();
        updateActiveTrackRowHighlight();
    });

    audio.addEventListener('ended', () => {
        playNext();
    });

    audio.addEventListener('error', (e) => {
        console.error("Audio playback error on track:", state.currentTrack, e);
        state.isPlaying = false;
        updatePlayPauseUI();
    });

    // ==========================================
    // CONTROLS INTERACTION
    // ==========================================
    btnPlayPause.addEventListener('click', togglePlayPause);
    btnPrev.addEventListener('click', playPrevious);
    btnNext.addEventListener('click', playNext);
    btnShuffle.addEventListener('click', toggleShuffle);
    btnRepeat.addEventListener('click', toggleRepeat);

    seekSlider.addEventListener('input', () => {
        if (!audio.duration) return;
        const seekRatio = seekSlider.value / 100;
        audio.currentTime = seekRatio * audio.duration;
        saveSessionState();
    });

    volumeSlider.addEventListener('input', () => {
        const val = volumeSlider.value / 100;
        state.volume = val;
        audio.volume = val;
        state.isMuted = val === 0;
        updateVolumeIcon();
        try {
            localStorage.setItem('wavify_web_player_volume', val.toString());
        } catch (e) {}
    });

    volumeIcon.addEventListener('click', () => {
        if (state.isMuted) {
            audio.volume = state.unmutedVolume || 1.0;
            state.volume = audio.volume;
            state.isMuted = false;
            volumeSlider.value = state.volume * 100;
        } else {
            state.unmutedVolume = state.volume > 0 ? state.volume : 1.0;
            audio.volume = 0;
            state.volume = 0;
            state.isMuted = true;
            volumeSlider.value = 0;
        }
        updateVolumeIcon();
    });

    function updateVolumeIcon() {
        if (state.isMuted || state.volume === 0) {
            volumeIcon.innerHTML = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><line x1="1" y1="1" x2="23" y2="23"></line><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"></path><path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>`;
        } else if (state.volume < 0.5) {
            volumeIcon.innerHTML = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M15.54 8.46a5 5 0 0 1 0 7.07"></path></svg>`;
        } else {
            volumeIcon.innerHTML = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"></path></svg>`;
        }
    }

    btnToggleQueue.addEventListener('click', () => {
        const willOpen = !queueDrawer.classList.contains('open');
        if (willOpen && nowPlayingPanel) nowPlayingPanel.classList.remove('open');
        queueDrawer.classList.toggle('open');
    });

    btnCloseQueue.addEventListener('click', () => {
        queueDrawer.classList.remove('open');
    });

    if (btnToggleNP) {
        btnToggleNP.addEventListener('click', () => {
            if (nowPlayingPanel) {
                const willOpen = !nowPlayingPanel.classList.contains('open');
                if (willOpen && queueDrawer) queueDrawer.classList.remove('open');
                nowPlayingPanel.classList.toggle('open');
            }
        });
    }

    if (btnCloseNP) {
        btnCloseNP.addEventListener('click', () => {
            if (nowPlayingPanel) nowPlayingPanel.classList.remove('open');
        });
    }

    if (pbArtwork) {
        pbArtwork.addEventListener('click', () => {
            if (nowPlayingPanel) {
                const willOpen = !nowPlayingPanel.classList.contains('open');
                if (willOpen && queueDrawer) queueDrawer.classList.remove('open');
                nowPlayingPanel.classList.toggle('open');
            }
        });
    }

    btnClearQueue.addEventListener('click', () => {
        if (state.currentTrack) {
            state.queue = [state.currentTrack];
            state.queueIndex = 0;
            renderQueue();
            saveSessionState();
        }
    });

    // Now Playing Quick Action Buttons
    if (btnNpPlayNext) {
        btnNpPlayNext.addEventListener('click', () => {
            if (state.currentTrack) {
                playNextTrack(state.currentTrack);
            }
        });
    }

    if (btnNpAddQueue) {
        btnNpAddQueue.addEventListener('click', () => {
            if (state.currentTrack) {
                addToQueue(state.currentTrack);
            }
        });
    }

    // Context Menu Event Wiring
    document.addEventListener('click', (e) => {
        if (trackContextMenu && !trackContextMenu.contains(e.target)) {
            closeContextMenu();
        }
    });
    window.addEventListener('scroll', closeContextMenu, true);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeContextMenu();
    });

    if (trackContextMenu) {
        trackContextMenu.querySelectorAll('.context-menu-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.stopPropagation();
                const action = item.getAttribute('data-action');
                if (activeMenuTrack) {
                    if (action === 'play-next') {
                        playNextTrack(activeMenuTrack);
                    } else if (action === 'add-queue') {
                        addToQueue(activeMenuTrack);
                    }
                }
                closeContextMenu();
            });
        });
    }

    window.addEventListener('beforeunload', saveSessionState);

    // Keyboard Shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
        if (e.code === 'Space') {
            e.preventDefault();
            togglePlayPause();
        } else if (e.code === 'ArrowRight' && e.shiftKey) {
            e.preventDefault();
            playNext();
        } else if (e.code === 'ArrowLeft' && e.shiftKey) {
            e.preventDefault();
            playPrevious();
        } else if (e.code === 'ArrowRight') {
            audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5);
        } else if (e.code === 'ArrowLeft') {
            audio.currentTime = Math.max(0, audio.currentTime - 5);
        }
    });

    // ==========================================
    // DATA LOADING & RENDERING
    // ==========================================
    // SMART SHELVES & HOME VIEW RENDERING
    // ==========================================
    function createTrackShelfCard(track, contextTracks, contextName) {
        const card = document.createElement('div');
        card.className = 'shelf-card';
        const fileId = track.driveFileId || track.file_id || track.id;
        card.setAttribute('data-id', fileId);

        const artUrl = track.album_art || track.albumArt || "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='1'%3E%3Ccircle cx='12' cy='12' r='4'/%3E%3C/svg%3E";

        card.innerHTML = `
            <div class="shelf-card-art-wrap">
                <img class="shelf-card-art" src="${escapeHTML(getThumbnailUrl(artUrl))}" loading="lazy" decoding="async" alt="">
                <button class="card-play-btn" title="Play ${escapeHTML(track.title || 'Track')}" aria-label="Play ${escapeHTML(track.title || 'Track')}">
                    <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>
                </button>
            </div>
            <div class="shelf-card-info">
                <div class="shelf-card-title">${escapeHTML(track.title || 'Unknown Title')}</div>
                <div class="shelf-card-sub">${escapeHTML(track.artist || 'Unknown Artist')}</div>
            </div>
        `;

        const playBtn = card.querySelector('.card-play-btn');
        playBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const tId = String(track.driveFileId || track.file_id || track.id);
            const idx = contextTracks.findIndex(t => String(t.driveFileId || t.file_id || t.id) === tId);
            startPlaybackContext('shelf', contextName, contextTracks, Math.max(0, idx));
        });

        card.addEventListener('click', () => {
            const tId = String(track.driveFileId || track.file_id || track.id);
            const idx = contextTracks.findIndex(t => String(t.driveFileId || t.file_id || t.id) === tId);
            startPlaybackContext('shelf', contextName, contextTracks, Math.max(0, idx));
        });

        return card;
    }

    function renderRecentlyPlayedShelf() {
        if (!shelfRecentlyPlayed || !shelfRecentlyPlayedWrap) return;
        if (!state.recentHistory || state.recentHistory.length === 0) {
            shelfRecentlyPlayedWrap.style.display = 'none';
            return;
        }
        shelfRecentlyPlayedWrap.style.display = 'block';
        shelfRecentlyPlayed.innerHTML = '';
        const displayList = state.recentHistory.slice(0, 8);
        displayList.forEach(track => {
            const card = createTrackShelfCard(track, displayList, 'Recently Played');
            shelfRecentlyPlayed.appendChild(card);
        });
    }

    function renderRecentlyAddedShelf() {
        if (!shelfRecentlyAdded) return;
        if (!state.allTracks || state.allTracks.length === 0) {
            shelfRecentlyAdded.innerHTML = '<div class="shelf-loading">Loading recently added tracks...</div>';
            return;
        }
        // Sort by timestamp descending
        const sorted = [...state.allTracks].sort((a, b) => {
            const timeA = a.timestamp ? new Date(a.timestamp).getTime() : 0;
            const timeB = b.timestamp ? new Date(b.timestamp).getTime() : 0;
            return timeB - timeA;
        });
        const recentAdded = sorted.slice(0, 10);
        shelfRecentlyAdded.innerHTML = '';
        recentAdded.forEach(track => {
            const card = createTrackShelfCard(track, recentAdded, 'Recently Added');
            shelfRecentlyAdded.appendChild(card);
        });
    }

    function renderTopArtistsShelf() {
        if (!shelfTopArtists) return;
        if (!state.allArtists || state.allArtists.length === 0) {
            shelfTopArtists.innerHTML = '<div class="shelf-loading">Loading top artists...</div>';
            return;
        }
        // Sort by track_count descending
        const sorted = [...state.allArtists].sort((a, b) => (b.track_count || 0) - (a.track_count || 0));
        const topArtists = sorted.slice(0, 8);
        shelfTopArtists.innerHTML = '';
        topArtists.forEach(artist => {
            const card = document.createElement('div');
            card.className = 'shelf-card artist-card-home';
            const artUrl = artist.cover_image || "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='1'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M12 8v4l3 3'/%3E%3C/svg%3E";

            card.innerHTML = `
                <div class="shelf-card-art-wrap">
                    <img class="shelf-card-art" src="${escapeHTML(getThumbnailUrl(artUrl))}" loading="lazy" decoding="async" alt="">
                    <button class="card-play-btn" title="Play ${escapeHTML(artist.artist_name)}" aria-label="Play ${escapeHTML(artist.artist_name)}">
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>
                    </button>
                </div>
                <div class="shelf-card-info">
                    <div class="shelf-card-title">${escapeHTML(artist.artist_name)}</div>
                    <div class="shelf-card-sub">${artist.track_count} Track${artist.track_count !== 1 ? 's' : ''}</div>
                </div>
            `;

            const playBtn = card.querySelector('.card-play-btn');
            playBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                let aTracks = [];
                if (state.allTracks && state.allTracks.length > 0) {
                    const targetLower = artist.artist_name.trim().toLowerCase();
                    aTracks = state.allTracks.filter(t => {
                        const a = (t.artist || '').toLowerCase();
                        const parts = a.split(',').map(s => s.trim());
                        return parts.includes(targetLower) || a === targetLower || a.includes(targetLower);
                    });
                }
                if (aTracks.length > 0) {
                    state.shuffle = false;
                    btnShuffle.classList.remove('active');
                    startPlaybackContext('artist', `Artist: ${artist.artist_name}`, aTracks, 0);
                } else {
                    openArtistDetail(artist.artist_name, artist.cover_image);
                }
            });

            card.addEventListener('click', () => {
                openArtistDetail(artist.artist_name, artist.cover_image);
            });

            shelfTopArtists.appendChild(card);
        });
    }

    function renderFeaturedPlaylistsShelf() {
        if (!shelfFeaturedPlaylists) return;
        if (!state.allPlaylists || state.allPlaylists.length === 0) {
            shelfFeaturedPlaylists.innerHTML = '<div class="shelf-loading">Loading playlists...</div>';
            return;
        }
        const topPlaylists = state.allPlaylists.slice(0, 8);
        shelfFeaturedPlaylists.innerHTML = '';
        topPlaylists.forEach(pl => {
            const card = document.createElement('div');
            card.className = 'shelf-card';
            const trackCount = pl.total_tracks != null ? pl.total_tracks : (pl.track_ids ? pl.track_ids.length : (pl.tracks ? pl.tracks.length : (pl.track_count || 0)));
            const coverImg = pl.cover_image || pl.coverImage;
            const coverHtml = coverImg
                ? `<img class="shelf-card-art" src="${escapeHTML(getThumbnailUrl(coverImg))}" loading="lazy" decoding="async" alt="">`
                : `<div style="width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; font-size: 32px; background: var(--bg-card-hover);">🎵</div>`;

            card.innerHTML = `
                <div class="shelf-card-art-wrap">
                    ${coverHtml}
                    <button class="card-play-btn" title="Play ${escapeHTML(pl.name || 'Playlist')}" aria-label="Play ${escapeHTML(pl.name || 'Playlist')}">
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>
                    </button>
                </div>
                <div class="shelf-card-info">
                    <div class="shelf-card-title">${escapeHTML(pl.name || pl.title || 'Playlist')}</div>
                    <div class="shelf-card-sub">${trackCount} Track${trackCount !== 1 ? 's' : ''}</div>
                </div>
            `;

            const playBtn = card.querySelector('.card-play-btn');
            playBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                let pTracks = [];
                if (pl.tracks && pl.tracks.length > 0) {
                    pTracks = pl.tracks;
                } else if (pl.track_ids && state.allTracks && state.allTracks.length > 0) {
                    const tMap = new Map();
                    state.allTracks.forEach(t => {
                        const k1 = t.driveFileId ? String(t.driveFileId) : null;
                        const k2 = t.id ? String(t.id) : null;
                        const k3 = t.file_id ? String(t.file_id) : null;
                        if (k1) tMap.set(k1, t);
                        if (k2) tMap.set(k2, t);
                        if (k3) tMap.set(k3, t);
                    });
                    pl.track_ids.forEach(tid => {
                        const tr = tMap.get(String(tid));
                        if (tr) pTracks.push(tr);
                    });
                }
                if (pTracks.length > 0) {
                    state.shuffle = false;
                    btnShuffle.classList.remove('active');
                    startPlaybackContext('playlist', `Playlist: ${pl.name || 'Playlist'}`, pTracks, 0);
                } else {
                    openPlaylistDetail(pl);
                }
            });

            card.addEventListener('click', () => {
                openPlaylistDetail(pl);
            });

            shelfFeaturedPlaylists.appendChild(card);
        });
    }

    function renderHomeView() {
        renderRecentlyPlayedShelf();
        renderRecentlyAddedShelf();
        renderTopArtistsShelf();
        renderFeaturedPlaylistsShelf();
    }

    // ==========================================
    // DATA LOADING & RENDERING
    // ==========================================
    async function loadAllTracks() {
        try {
            const res = await fetch('/api/tracks');
            if (!res.ok) throw new Error("Failed to load tracks");
            state.allTracks = await res.json();
            renderTracksView();
            renderRecentlyAddedShelf();
            renderRecentlyPlayedShelf();
        } catch (e) {
            tracksTbody.innerHTML = `<tr><td colspan="6" class="table-placeholder" style="color: var(--status-red);">Error loading tracks: ${escapeHTML(e.message)}</td></tr>`;
        }
    }

    async function loadAllArtists() {
        try {
            const res = await fetch('/api/artists');
            if (!res.ok) throw new Error("Failed to load artists");
            state.allArtists = await res.json();
            renderArtistsGrid();
            renderTopArtistsShelf();
        } catch (e) {
            artistsGrid.innerHTML = `<div style="color: var(--status-red);">Error loading artists</div>`;
        }
    }

    async function loadAllPlaylists() {
        try {
            const res = await fetch('/api/playlists');
            if (!res.ok) throw new Error("Failed to load playlists");
            state.allPlaylists = await res.json();
            renderPlaylistsGrid();
            renderFeaturedPlaylistsShelf();
        } catch (e) {
            playlistsGrid.innerHTML = `<div style="color: var(--status-red);">Error loading playlists</div>`;
        }
    }

    function renderTracksView() {
        const query = (searchInput ? searchInput.value : '').trim().toLowerCase();
        const filtered = state.allTracks.filter(t => {
            const title = (t.title || '').toLowerCase();
            const artist = (t.artist || '').toLowerCase();
            const album = (t.album || '').toLowerCase();
            return title.includes(query) || artist.includes(query) || album.includes(query);
        });

        if (filtered.length === 0) {
            tracksTbody.innerHTML = `<tr><td colspan="6" class="table-placeholder">${query ? 'No matching tracks found.' : 'No tracks in library.'}</td></tr>`;
            return;
        }

        tracksTbody.innerHTML = '';
        filtered.forEach((track, idx) => {
            const tr = document.createElement('tr');
            tr.className = 'player-track-row';
            const fileId = track.driveFileId || track.file_id || track.id;
            tr.setAttribute('data-id', fileId);
            tr.setAttribute('data-index', idx + 1);

            const artUrl = track.album_art || track.albumArt || "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='44' height='44' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='1.5'%3E%3Ccircle cx='12' cy='12' r='4'/%3E%3C/svg%3E";

            tr.innerHTML = `
                <td style="width: 44px; text-align: center;" class="track-num-cell">${idx + 1}</td>
                <td style="width: 52px;"><img src="${escapeHTML(getThumbnailUrl(artUrl))}" loading="lazy" decoding="async" style="width: 44px; height: 44px; border-radius: 6px; object-fit: cover;" alt=""></td>
                <td class="title-cell-wrap">
                    <span class="track-title-bold">${escapeHTML(track.title || 'Unknown Title')}</span>
                    <span class="track-artist-small">${escapeHTML(track.artist || 'Unknown Artist')}</span>
                </td>
                <td>${escapeHTML(track.album || 'Unknown Album')}</td>
                <td style="text-align: right; width: 70px;">${track.duration || '--:--'}</td>
                <td style="width: 44px; text-align: center;">
                    <button class="track-menu-btn" title="More options" aria-label="More options">
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
                    </button>
                </td>
            `;

            tr.addEventListener('click', (e) => {
                if (e.target.closest('.track-menu-btn')) return;
                startPlaybackContext('library', 'All Tracks', filtered, idx);
            });

            const menuBtn = tr.querySelector('.track-menu-btn');
            if (menuBtn) {
                menuBtn.addEventListener('click', (e) => {
                    openContextMenu(e, track);
                });
            }

            tr.addEventListener('contextmenu', (e) => {
                openContextMenu(e, track);
            });

            tracksTbody.appendChild(tr);
        });

        updateActiveTrackRowHighlight();
    }

    if (searchInput) {
        searchInput.addEventListener('input', renderTracksView);
    }

    function renderArtistsGrid() {
        if (!artistsGrid) return;
        if (state.allArtists.length === 0) {
            artistsGrid.innerHTML = '<div style="color: var(--text-muted);">No artists found.</div>';
            return;
        }

        artistsGrid.innerHTML = '';
        state.allArtists.forEach(artist => {
            const card = document.createElement('div');
            card.className = 'card artist-card';
            card.style.cursor = 'pointer';
            card.style.display = 'flex';
            card.style.flexDirection = 'column';
            card.style.alignItems = 'center';
            card.style.textAlign = 'center';
            card.style.padding = '20px';
            card.style.transition = 'transform 0.2s, background 0.2s';
            card.style.borderRadius = '12px';

            const artUrl = artist.cover_image || "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='90' height='90' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='1'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M12 8v4l3 3'/%3E%3C/svg%3E";

            card.innerHTML = `
                <img src="${escapeHTML(getThumbnailUrl(artUrl))}" loading="lazy" decoding="async" alt="" style="width: 90px; height: 90px; border-radius: 50%; object-fit: cover; margin-bottom: 12px; border: 2px solid var(--border-color); box-shadow: var(--shadow-sm);">
                <h4 style="margin: 0 0 4px 0; font-size: 1rem;">${escapeHTML(artist.artist_name)}</h4>
                <span class="badge" style="background: var(--bg-card-hover); color: var(--text-muted); padding: 2px 8px; border-radius: 12px; font-size: 0.8rem;">${artist.track_count} Track${artist.track_count !== 1 ? 's' : ''}</span>
            `;

            card.addEventListener('click', () => {
                openArtistDetail(artist.artist_name, artist.cover_image);
            });

            artistsGrid.appendChild(card);
        });
    }

    async function openArtistDetail(artistName, coverImage) {
        switchTab('artists');
        artistsGrid.style.display = 'none';
        artistDetailView.style.display = 'block';

        artistDetailName.textContent = artistName;
        artistDetailImage.src = coverImage || "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='1'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3C/svg%3E";

        let tracks = [];
        if (state.allTracks && state.allTracks.length > 0) {
            const targetLower = artistName.trim().toLowerCase();
            tracks = state.allTracks.filter(t => {
                const a = (t.artist || '').toLowerCase();
                const parts = a.split(',').map(s => s.trim());
                return parts.includes(targetLower) || a === targetLower || a.includes(targetLower);
            });
        }

        if (tracks.length === 0) {
            artistDetailTracks.innerHTML = '<div style="padding: 16px 0; color: var(--text-muted);">Loading artist tracks...</div>';
            try {
                const res = await fetch(`/api/artists/${encodeURIComponent(artistName)}`);
                if (!res.ok) throw new Error("Failed to load tracks");
                tracks = await res.json();
            } catch (e) {
                artistDetailTracks.innerHTML = `<div style="color: var(--status-red);">Error loading artist tracks: ${escapeHTML(e.message)}</div>`;
                return;
            }
        }

        artistDetailCount.textContent = `${tracks.length} Track${tracks.length !== 1 ? 's' : ''}`;
        artistDetailTracks.innerHTML = '';
        tracks.forEach((track, idx) => {
            const row = document.createElement('div');
            row.className = 'player-track-row';
            const fileId = track.driveFileId || track.file_id || track.id;
            row.setAttribute('data-id', fileId);
            row.style.display = 'flex';
            row.style.alignItems = 'center';
            row.style.gap = '16px';
            row.style.padding = '12px 16px';
            row.style.borderRadius = '8px';
            row.style.border = '1px solid var(--border-color)';
            row.style.marginBottom = '8px';

            const artUrl = track.album_art || track.albumArt || '';

            row.innerHTML = `
                <span style="font-size: 0.85rem; color: var(--text-muted); width: 24px;">${idx + 1}</span>
                <img src="${escapeHTML(getThumbnailUrl(artUrl))}" loading="lazy" decoding="async" style="width: 44px; height: 44px; border-radius: 6px; object-fit: cover;" alt="">
                <div style="flex: 1; overflow: hidden;">
                    <div class="track-title-bold" style="font-weight: 500;">${escapeHTML(track.title)}</div>
                    <div style="font-size: 0.82rem; color: var(--text-muted);">${escapeHTML(track.album || '')}</div>
                </div>
                <div style="font-size: 0.82rem; color: var(--text-muted);">${track.duration || '--:--'}</div>
                <button class="track-menu-btn" title="More options" aria-label="More options" style="margin-left: 8px;">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
                </button>
            `;

            row.addEventListener('click', (e) => {
                if (e.target.closest('.track-menu-btn')) return;
                startPlaybackContext('artist', `Artist: ${artistName}`, tracks, idx);
            });

            const menuBtn = row.querySelector('.track-menu-btn');
            if (menuBtn) {
                menuBtn.addEventListener('click', (e) => {
                    openContextMenu(e, track);
                });
            }

            row.addEventListener('contextmenu', (e) => {
                openContextMenu(e, track);
            });

            artistDetailTracks.appendChild(row);
        });

        btnPlayArtist.onclick = () => {
            state.shuffle = false;
            btnShuffle.classList.remove('active');
            btnShuffle.title = "Shuffle: Off";
            startPlaybackContext('artist', `Artist: ${artistName}`, tracks, 0);
        };

        btnShuffleArtist.onclick = () => {
            state.shuffle = true;
            btnShuffle.classList.add('active');
            btnShuffle.title = "Shuffle: On";
            const randomIdx = tracks.length > 0 ? Math.floor(Math.random() * tracks.length) : 0;
            startPlaybackContext('artist', `Artist: ${artistName}`, tracks, randomIdx);
        };
    }

    if (btnBackArtists) {
        btnBackArtists.addEventListener('click', () => {
            artistDetailView.style.display = 'none';
            artistsGrid.style.display = 'grid';
        });
    }

    function renderPlaylistsGrid() {
        if (!playlistsGrid) return;
        if (state.allPlaylists.length === 0) {
            playlistsGrid.innerHTML = '<div style="color: var(--text-muted);">No playlists found.</div>';
            return;
        }

        playlistsGrid.innerHTML = '';
        state.allPlaylists.forEach(pl => {
            const card = document.createElement('div');
            card.className = 'card playlist-card';
            card.style.cursor = 'pointer';
            card.style.padding = '20px';
            card.style.borderRadius = '12px';
            card.style.display = 'flex';
            card.style.flexDirection = 'column';
            card.style.gap = '8px';

            const trackCount = pl.total_tracks != null ? pl.total_tracks : (pl.track_ids ? pl.track_ids.length : (pl.tracks ? pl.tracks.length : (pl.track_count || 0)));
            const coverImg = pl.cover_image || pl.coverImage;
            const coverHtml = coverImg
                ? `<img src="${escapeHTML(getThumbnailUrl(coverImg))}" loading="lazy" decoding="async" style="width: 100%; aspect-ratio: 1; border-radius: 8px; object-fit: cover; border: 1px solid var(--border-color);" alt="">`
                : `<div style="width: 100%; aspect-ratio: 1; border-radius: 8px; background: var(--bg-card-hover); display: flex; align-items: center; justify-content: center; font-size: 32px; border: 1px solid var(--border-color);">🎵</div>`;

            card.innerHTML = `
                ${coverHtml}
                <h4 style="margin: 8px 0 2px 0; font-size: 1rem;">${escapeHTML(pl.name || pl.title || 'Playlist')}</h4>
                <span class="badge" style="background: var(--bg-card-hover); color: var(--text-muted); padding: 2px 8px; border-radius: 12px; font-size: 0.8rem; align-self: flex-start;">${trackCount} Tracks</span>
            `;

            card.addEventListener('click', () => {
                openPlaylistDetail(pl);
            });

            playlistsGrid.appendChild(card);
        });
    }

    function renderPlaylistTracksView(tracks, playlistName) {
        playlistDetailCount.textContent = `${tracks.length} Track${tracks.length !== 1 ? 's' : ''}`;
        playlistDetailTracks.innerHTML = '';

        if (tracks.length === 0) {
            playlistDetailTracks.innerHTML = '<div style="padding: 16px 0; color: var(--text-muted);">No tracks in this playlist.</div>';
            return;
        }

        tracks.forEach((track, idx) => {
            const row = document.createElement('div');
            row.className = 'player-track-row';
            const fileId = track.driveFileId || track.file_id || track.id;
            row.setAttribute('data-id', fileId);
            row.style.display = 'flex';
            row.style.alignItems = 'center';
            row.style.gap = '16px';
            row.style.padding = '12px 16px';
            row.style.borderRadius = '8px';
            row.style.border = '1px solid var(--border-color)';
            row.style.marginBottom = '8px';

            const artUrl = track.album_art || track.albumArt || '';

            row.innerHTML = `
                <span style="font-size: 0.85rem; color: var(--text-muted); width: 24px;">${idx + 1}</span>
                <img src="${escapeHTML(getThumbnailUrl(artUrl))}" loading="lazy" decoding="async" style="width: 44px; height: 44px; border-radius: 6px; object-fit: cover;" alt="">
                <div style="flex: 1; overflow: hidden;">
                    <div class="track-title-bold" style="font-weight: 500;">${escapeHTML(track.title || 'Unknown Title')}</div>
                    <div style="font-size: 0.82rem; color: var(--text-muted);">${escapeHTML(track.artist || 'Unknown Artist')}</div>
                </div>
                <div style="font-size: 0.82rem; color: var(--text-muted);">${track.duration || '--:--'}</div>
                <button class="track-menu-btn" title="More options" aria-label="More options" style="margin-left: 8px;">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
                </button>
            `;

            row.addEventListener('click', (e) => {
                if (e.target.closest('.track-menu-btn')) return;
                startPlaybackContext('playlist', `Playlist: ${playlistName}`, tracks, idx);
            });

            const menuBtn = row.querySelector('.track-menu-btn');
            if (menuBtn) {
                menuBtn.addEventListener('click', (e) => {
                    openContextMenu(e, track);
                });
            }

            row.addEventListener('contextmenu', (e) => {
                openContextMenu(e, track);
            });

            playlistDetailTracks.appendChild(row);
        });

        btnPlayPlaylist.onclick = () => {
            state.shuffle = false;
            btnShuffle.classList.remove('active');
            btnShuffle.title = "Shuffle: Off";
            startPlaybackContext('playlist', `Playlist: ${playlistName}`, tracks, 0);
        };

        btnShufflePlaylist.onclick = () => {
            state.shuffle = true;
            btnShuffle.classList.add('active');
            btnShuffle.title = "Shuffle: On";
            const randomIdx = tracks.length > 0 ? Math.floor(Math.random() * tracks.length) : 0;
            startPlaybackContext('playlist', `Playlist: ${playlistName}`, tracks, randomIdx);
        };

        updateActiveTrackRowHighlight();
    }

    async function openPlaylistDetail(playlist) {
        switchTab('playlists');
        playlistsGrid.style.display = 'none';
        playlistDetailView.style.display = 'block';

        const name = playlist.name || playlist.title || 'Playlist';
        playlistDetailName.textContent = name;

        if (playlistDetailArt) {
            const cover = playlist.cover_image || playlist.coverImage;
            if (cover) {
                playlistDetailArt.src = getThumbnailUrl(cover);
            } else {
                playlistDetailArt.src = "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='100' viewBox='0 0 24 24' fill='none' stroke='%238a8377' stroke-width='1.5'%3E%3Crect x='3' y='3' width='18' height='18' rx='2'/%3E%3Ccircle cx='12' cy='12' r='4'/%3E%3C/svg%3E";
            }
        }

        // Try in-memory resolution first (instantaneous 0 ms)
        let resolvedTracks = [];
        if (playlist.tracks && playlist.tracks.length > 0) {
            resolvedTracks = playlist.tracks;
        } else if (playlist.track_ids && playlist.track_ids.length > 0 && state.allTracks && state.allTracks.length > 0) {
            const trackMap = new Map();
            state.allTracks.forEach(t => {
                const key1 = t.driveFileId ? String(t.driveFileId) : null;
                const key2 = t.id ? String(t.id) : null;
                const key3 = t.file_id ? String(t.file_id) : null;
                if (key1) trackMap.set(key1, t);
                if (key2) trackMap.set(key2, t);
                if (key3) trackMap.set(key3, t);
            });
            playlist.track_ids.forEach(tid => {
                const tr = trackMap.get(String(tid));
                if (tr) resolvedTracks.push(tr);
            });
        }

        if (resolvedTracks.length > 0) {
            renderPlaylistTracksView(resolvedTracks, name);
            return;
        }

        // Fallback to network if tracks not yet in memory
        playlistDetailCount.textContent = 'Loading...';
        playlistDetailTracks.innerHTML = '<div style="padding: 16px 0; color: var(--text-muted);">Loading playlist tracks...</div>';

        const plId = playlist.id || playlist.playlist_id;

        try {
            const res = await fetch(`/api/playlists/${encodeURIComponent(plId)}`);
            if (!res.ok) throw new Error("Failed to load playlist");
            const data = await res.json();
            const tracks = data.tracks || [];
            renderPlaylistTracksView(tracks, name);
        } catch (e) {
            playlistDetailTracks.innerHTML = `<div style="color: var(--status-red);">Error loading playlist: ${escapeHTML(e.message)}</div>`;
        }
    }

    if (btnBackPlaylists) {
        btnBackPlaylists.addEventListener('click', () => {
            playlistDetailView.style.display = 'none';
            playlistsGrid.style.display = 'grid';
        });
    }

    // Tab Navigation
    function switchTab(targetTab) {
        state.activeTab = targetTab;

        tabBtns.forEach(b => {
            b.classList.toggle('active', b.getAttribute('data-tab') === targetTab);
        });

        if (viewHome) viewHome.style.display = targetTab === 'home' ? 'block' : 'none';
        if (viewTracks) viewTracks.style.display = targetTab === 'tracks' ? 'block' : 'none';
        if (viewArtists) viewArtists.style.display = targetTab === 'artists' ? 'block' : 'none';
        if (viewPlaylists) viewPlaylists.style.display = targetTab === 'playlists' ? 'block' : 'none';

        if (targetTab === 'home') {
            renderHomeView();
        } else if (targetTab === 'playlists') {
            if (playlistDetailView) playlistDetailView.style.display = 'none';
            if (playlistsGrid) playlistsGrid.style.display = 'grid';
            if (state.allPlaylists.length === 0) {
                loadAllPlaylists();
            }
        } else if (targetTab === 'artists') {
            if (artistDetailView) artistDetailView.style.display = 'none';
            if (artistsGrid) artistsGrid.style.display = 'grid';
            if (state.allArtists.length === 0) {
                loadAllArtists();
            }
        }
    }

    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-tab');
            switchTab(targetTab);
        });
    });

    if (seeAllRecent) seeAllRecent.addEventListener('click', () => switchTab('tracks'));
    if (seeAllArtists) seeAllArtists.addEventListener('click', () => switchTab('artists'));
    if (seeAllPlaylists) seeAllPlaylists.addEventListener('click', () => switchTab('playlists'));

    // Bootstrap
    restoreSessionState();
    loadAllTracks();
    loadAllArtists();
    loadAllPlaylists();
    renderRecentlyPlayedShelf();
});
