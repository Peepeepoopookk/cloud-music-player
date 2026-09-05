document.addEventListener('DOMContentLoaded', () => {
    // Global State
    let allTracks = [];
    let searchQuery = '';
    let sortColumn = 'index';
    let sortDirection = 'asc';
    let trackToDeleteFileId = null;

    // Global Background status tracking variables
    let backfillRunning = false;
    let singleAddRunning = false;
    let scraperRunning = false;

    // Navigation handlers
    const navItems = document.querySelectorAll('.nav-item');
    const sections = document.querySelectorAll('.content-section');
    
    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const targetId = item.getAttribute('data-target');
            
            // Toggle active state in nav
            navItems.forEach(nav => nav.classList.remove('active'));
            item.classList.add('active');
            
            // Toggle active state in sections
            sections.forEach(sec => {
                sec.classList.remove('active');
                if (sec.id === targetId) {
                    sec.classList.add('active');
                }
            });
            
            // Reload specific section data
            if (targetId === 'section-library') {
                loadTracks(false);
            } else if (targetId === 'section-storage') {
                loadStorage();
            } else if (targetId === 'section-settings') {
                loadConfig();
            } else if (targetId === 'section-downloader') {
                loadDownloadLogs();
                loadPlaylistLogs();
                if (window.backgroundStatus) {
                    syncDownloaderUI(window.backgroundStatus);
                }
            } else if (targetId === 'section-app-imports') {
                loadAppImports();
            } else if (targetId === 'section-artists') {
                loadArtists();
            } else if (targetId === 'section-data-health') {
                loadDataHealth();
            }
        });
    });

    // Toast Notifications System
    function showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        if (!container) return;
        
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        
        let iconSvg = '';
        if (type === 'success') {
            iconSvg = `<svg class="toast-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
        } else if (type === 'error') {
            iconSvg = `<svg class="toast-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>`;
        } else {
            // info
            iconSvg = `<svg class="toast-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`;
        }
        
        toast.innerHTML = `
            <div class="toast-icon-wrapper">${iconSvg}</div>
            <div class="toast-content">${escapeHTML(message)}</div>
            <button class="toast-close">&times;</button>
        `;
        
        container.appendChild(toast);
        
        // Force reflow and show
        toast.offsetHeight;
        toast.classList.add('show');
        
        const dismissToast = () => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 400);
        };
        
        toast.querySelector('.toast-close').addEventListener('click', dismissToast);
        setTimeout(dismissToast, 5000);
    }

    // Drive Connection Status Verification
    const statusDot = document.getElementById('status-dot');
    const statusLabel = document.getElementById('status-label');
    
    async function checkConnection() {
        try {
            const response = await fetch('/api/tracks');
            if (response.ok) {
                statusDot.className = 'status-indicator-dot online';
                statusLabel.textContent = 'Drive Connected';
            } else {
                throw new Error('Connection response invalid');
            }
        } catch (err) {
            statusDot.className = 'status-indicator-dot offline';
            statusLabel.textContent = 'Drive Disconnected';
        }
    }

    // Helper to parse duration string (MM:SS or HH:MM:SS) into seconds for sorting
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

    // Table rendering logic with search filter and column sorting
    const tracksTableBody = document.getElementById('tracks-table-body');
    const navbarSongBadge = document.getElementById('navbar-song-badge');
    const storageTotalTracks = document.getElementById('storage-total-tracks');

    function renderTracksTable() {
        // Apply search filter (title or artist)
        const query = searchQuery.trim().toLowerCase();
        let filteredTracks = allTracks.filter(track => {
            const title = (track.title || '').toLowerCase();
            const artist = (track.artist || '').toLowerCase();
            return title.includes(query) || artist.includes(query);
        });

        // Apply sorting
        filteredTracks.sort((a, b) => {
            let valA = a[sortColumn];
            let valB = b[sortColumn];

            if (sortColumn === 'index') {
                valA = a._originalIndex;
                valB = b._originalIndex;
            } else if (sortColumn === 'size') {
                valA = valA ? parseInt(valA) : 0;
                valB = valB ? parseInt(valB) : 0;
            } else if (sortColumn === 'duration') {
                valA = parseDuration(valA);
                valB = parseDuration(valB);
            } else if (sortColumn === 'timestamp') {
                const timeA = a.timestamp ? new Date(a.timestamp).getTime() : 0;
                const timeB = b.timestamp ? new Date(b.timestamp).getTime() : 0;
                valA = isNaN(timeA) ? 0 : timeA;
                valB = isNaN(timeB) ? 0 : timeB;
            } else {
                valA = (valA || '').toString().toLowerCase();
                valB = (valB || '').toString().toLowerCase();
            }

            if (valA < valB) return sortDirection === 'asc' ? -1 : 1;
            if (valA > valB) return sortDirection === 'asc' ? 1 : -1;
            return 0;
        });

        // Update counts
        const totalCount = allTracks.length;
        navbarSongBadge.textContent = totalCount;
        if (storageTotalTracks) {
            storageTotalTracks.textContent = totalCount;
        }

        const hasActions = !!document.querySelector('#tracks-table th.actions-col');
        if (filteredTracks.length === 0) {
            const colCount = hasActions ? 8 : 7;
            tracksTableBody.innerHTML = `<tr><td colspan="${colCount}" class="table-placeholder">${query ? 'No matching tracks found.' : 'No tracks found.'}</td></tr>`;
            return;
        }

        tracksTableBody.innerHTML = '';
        filteredTracks.forEach(track => {
            const tr = document.createElement('tr');
            
            const title = track.title || 'Unknown Title';
            const artist = track.artist || 'Unknown Artist';
            const album = track.album || 'Unknown Album';
            const duration = track.duration || '--:--';
            let dateAdded = 'Unknown';
            if (track.timestamp) {
                const dateObj = new Date(track.timestamp);
                if (!isNaN(dateObj.getTime())) {
                    dateAdded = dateObj.toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' });
                }
            }
            const fileId = track.driveFileId || track.file_id || track.id;
            const sizeFormatted = track.size ? formatBytes(parseInt(track.size)) : '--';
            const trackNum = track._originalIndex + 1;

            const albumArtUrl = track.album_art || track.albumArt;
            let artHtml = '';
            if (albumArtUrl) {
                artHtml = `<img class="track-artwork" src="${escapeHTML(albumArtUrl)}" alt="${escapeHTML(title)}" loading="lazy">`;
            } else {
                artHtml = `<div class="track-art-placeholder">🎵</div>`;
            }

            const actionsHtml = hasActions ? `
                <td class="actions-col">
                    <button class="btn btn-danger btn-delete-track" data-id="${fileId}" data-title="${escapeHTML(title)}">Delete</button>
                </td>
            ` : '';

            tr.innerHTML = `
                <td class="artwork-col">${artHtml}</td>
                <td><span class="text-muted">${trackNum}</span></td>
                <td class="title-cell-wrap">
                    <span class="track-title-bold">${escapeHTML(title)}</span>
                    <span class="track-artist-small">${escapeHTML(artist)}</span>
                </td>
                <td>${escapeHTML(album)}</td>
                <td>${escapeHTML(duration)}</td>
                <td>${escapeHTML(dateAdded)}</td>
                <td>${sizeFormatted}</td>
                ${actionsHtml}
            `;
            tracksTableBody.appendChild(tr);
        });

        // Attach custom delete handlers
        document.querySelectorAll('.btn-delete-track').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const id = btn.getAttribute('data-id');
                const title = btn.getAttribute('data-title');
                openDeleteModal(id, title);
            });
        });
    }

    // Load tracks from the backend API
    async function loadTracks(showLoadingState = false) {
        try {
            if (showLoadingState) {
                tracksTableBody.innerHTML = '<tr><td colspan="8" class="table-placeholder">Loading tracks...</td></tr>';
            }
            const response = await fetch('/api/tracks');
            if (!response.ok) throw new Error('Failed to fetch tracks');
            
            const data = await response.json();
            
            let tracks = [];
            if (Array.isArray(data)) {
                tracks = data;
            } else if (data && typeof data === 'object') {
                if (Array.isArray(data.tracks)) {
                    tracks = data.tracks;
                } else {
                    tracks = Object.values(data);
                }
            }
            
            // Map original index to preserve numbering after filtering/sorting
            allTracks = tracks.map((track, index) => {
                track._originalIndex = index;
                return track;
            });

            renderTracksTable();
            
        } catch (err) {
            tracksTableBody.innerHTML = `<tr><td colspan="8" class="table-placeholder text-danger">Error: ${escapeHTML(err.message)}</td></tr>`;
        }
    }

    // Search input listener
    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            searchQuery = e.target.value;
            renderTracksTable();
        });
    }

    // Sorting headers listeners
    const sortableHeaders = document.querySelectorAll('th.sortable');
    sortableHeaders.forEach(th => {
        th.addEventListener('click', () => {
            const col = th.getAttribute('data-sort');
            
            // Toggle direction or set new column
            if (sortColumn === col) {
                sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                sortColumn = col;
                sortDirection = 'asc';
            }

            // Update DOM headers styling
            sortableHeaders.forEach(h => {
                h.classList.remove('asc', 'desc');
            });
            th.classList.add(sortDirection);

            renderTracksTable();
        });
    });

    // Custom confirm delete modal handlers
    const confirmModal = document.getElementById('confirm-modal');
    const modalTrackTitle = document.getElementById('modal-track-title');
    const modalBtnCancel = document.getElementById('modal-btn-cancel');
    const modalCancelX = document.getElementById('modal-cancel-x');
    const modalBtnConfirm = document.getElementById('modal-btn-confirm');

    function openDeleteModal(fileId, trackTitle) {
        trackToDeleteFileId = fileId;
        modalTrackTitle.textContent = trackTitle;
        confirmModal.classList.remove('hidden');
    }

    function closeDeleteModal() {
        trackToDeleteFileId = null;
        confirmModal.classList.add('hidden');
    }

    [modalBtnCancel, modalCancelX].forEach(el => {
        if (el) el.addEventListener('click', closeDeleteModal);
    });

    if (modalBtnConfirm) {
        modalBtnConfirm.addEventListener('click', async () => {
            if (trackToDeleteFileId) {
                const id = trackToDeleteFileId;
                closeDeleteModal();
                await deleteTrack(id);
            }
        });
    }

    // Close modal if clicked outside
    window.addEventListener('click', (e) => {
        if (e.target === confirmModal) {
            closeDeleteModal();
        }
    });

    // LIBRARY: Delete a specific track asynchronously
    async function deleteTrack(fileId) {
        try {
            const response = await fetch(`/api/delete/${fileId}`, {
                method: 'POST'
            });
            const resData = await response.json();
            if (response.ok && resData.status === 'success') {
                showToast('Track deleted from Drive successfully.', 'success');
                // Refresh data asynchronously
                loadTracks(false);
                loadStorage();
            } else {
                throw new Error(resData.error || 'Failed to delete track');
            }
        } catch (err) {
            showToast(`Delete failed: ${err.message}`, 'error');
        }
    }

    // LIBRARY: Scraper Execution triggers & Polling
    const btnRunScraper = document.getElementById('btn-run-scraper');
    const scraperSpinner = document.getElementById('scraper-spinner');
    const btnScraperText = document.getElementById('btn-scraper-text');
    
    if (btnRunScraper) {
        btnRunScraper.addEventListener('click', async () => {
            try {
                btnRunScraper.disabled = true;
                scraperSpinner.classList.remove('hidden');
                btnScraperText.textContent = 'Starting...';
                
                const response = await fetch('/api/scrape', {
                    method: 'POST'
                });
                const resData = await response.json();
                
                if (response.ok && resData.status === 'success') {
                    showToast('Backend scraper job started in the background.', 'info');
                    // Start status polling immediately
                    setTimeout(pollBackgroundStatus, 500);
                } else {
                    throw new Error(resData.error || 'Failed to trigger scraper script');
                }
            } catch (err) {
                showToast(`Scraper failed to start: ${err.message}`, 'error');
                btnRunScraper.disabled = false;
                scraperSpinner.classList.add('hidden');
                btnScraperText.textContent = 'Run Scraper';
            }
        });
    }

    // STORAGE: Load storage metrics
    const storageUsedSize = document.getElementById('storage-used-size');
    const storageDriveUsed = document.getElementById('storage-drive-used');
    const storageRemainingSize = document.getElementById('storage-remaining-size');
    const storageProgressFill = document.getElementById('storage-progress-fill');
    const usagePercentageLabel = document.getElementById('usage-percentage-label');
    const progressUsedText = document.getElementById('progress-used-text');
    const progressLimitText = document.getElementById('progress-limit-text');
    const dbUpdateTime = document.getElementById('db-update-time');
    
    async function loadStorage() {
        try {
            const response = await fetch('/api/storage');
            if (!response.ok) throw new Error('Failed to fetch storage statistics');
            const data = await response.json();
            
            // Format sizes
            const mediaBytes = data.media_size_bytes ?? 0;
            const driveLimitBytes = data.drive_limit_bytes ?? 0;
            const driveUsageBytes = data.drive_usage_bytes ?? 0;
            const driveRemainingBytes = Math.max(0, driveLimitBytes - driveUsageBytes);
            
            if (storageTotalTracks) {
                storageTotalTracks.textContent = data.total_tracks ?? allTracks.length;
            }
            if (storageUsedSize) {
                storageUsedSize.textContent = formatBytes(mediaBytes);
            }
            if (storageDriveUsed) {
                storageDriveUsed.textContent = formatBytes(driveUsageBytes);
            }
            if (storageRemainingSize) {
                storageRemainingSize.textContent = formatBytes(driveRemainingBytes);
            }
            if (progressUsedText) {
                progressUsedText.textContent = formatBytes(driveUsageBytes);
            }
            if (progressLimitText) {
                progressLimitText.textContent = formatBytes(driveLimitBytes);
            }
            
            // Update Visual Storage Usage Bar based on Google Drive quota
            if (driveLimitBytes > 0) {
                const percentUsed = ((driveUsageBytes / driveLimitBytes) * 100).toFixed(2);
                if (storageProgressFill) {
                    storageProgressFill.style.width = `${percentUsed}%`;
                }
                if (usagePercentageLabel) {
                    usagePercentageLabel.textContent = `${percentUsed}% Used`;
                }
            } else {
                if (storageProgressFill) {
                    storageProgressFill.style.width = '0%';
                }
                if (usagePercentageLabel) {
                    usagePercentageLabel.textContent = '0% Used';
                }
            }
            
            // Update Album Art Status coverage card
            const storageAlbumArtInfo = document.getElementById('storage-album-art-info');
            const storageAlbumArtSize = document.getElementById('storage-album-art-size');
            if (storageAlbumArtInfo) {
                const total = data.total_tracks ?? allTracks.length;
                const artCount = data.album_art_count ?? 0;
                storageAlbumArtInfo.textContent = `${artCount} / ${total} tracks have album art`;
                
                if (storageAlbumArtSize) {
                    const artBytes = data.album_art_storage_bytes ?? 0;
                    storageAlbumArtSize.textContent = `~${formatBytes(artBytes)} estimated`;
                }
            }

            // Update Last Modified Timestamp
            if (dbUpdateTime) {
                if (data.last_updated) {
                    const date = new Date(data.last_updated);
                    dbUpdateTime.innerHTML = `<span class="db-update-time">Last updated: ${date.toLocaleString()}</span>`;
                } else {
                    dbUpdateTime.innerHTML = '';
                }
            }
            
        } catch (err) {
            console.error('Failed to load storage stats:', err);
        }
    }

    // LOGS: Load terminal logs
    const logsOutputArea = document.getElementById('logs-output-area');
    const btnRefreshLogs = document.getElementById('btn-refresh-logs');
    const logsRefreshIcon = document.getElementById('logs-refresh-icon');
    
    async function loadLogs() {
        try {
            if (logsRefreshIcon) logsRefreshIcon.classList.add('icon-spin');
            if (btnRefreshLogs) btnRefreshLogs.disabled = true;
            
            const response = await fetch('/api/logs');
            if (!response.ok) throw new Error('Failed to fetch scraper log file');
            const data = await response.json();
            
            const logs = data.logs || [];
            if (logs.length === 0) {
                if (logsOutputArea) logsOutputArea.textContent = 'Log file empty or not yet generated.';
            } else {
                if (logsOutputArea) {
                    logsOutputArea.textContent = logs.join('\n');
                    // Auto-scroll to bottom
                    const termBody = document.querySelector('.terminal-body');
                    if (termBody) {
                        termBody.scrollTop = termBody.scrollHeight;
                    }
                }
            }
        } catch (err) {
            if (logsOutputArea) logsOutputArea.textContent = `Error loading system logs: ${err.message}`;
        } finally {
            if (logsRefreshIcon) logsRefreshIcon.classList.remove('icon-spin');
            if (btnRefreshLogs) btnRefreshLogs.disabled = false;
        }
    }
    
    if (btnRefreshLogs) {
        btnRefreshLogs.addEventListener('click', loadLogs);
    }

    // 30 Seconds Background Auto-Refresh
    setInterval(() => {
        // Only refresh tracks if the library section is active, to avoid background network noise
        const librarySection = document.getElementById('section-library');
        if (librarySection && librarySection.classList.contains('active')) {
            // Fetch tracks in-place without showing full loader, to prevent page flicker
            loadTracks(false);
            loadStorage();
        }
    }, 30000);

    // Utility formatting helpers
    function formatBytes(bytes, decimals = 2) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const dm = decimals < 0 ? 0 : decimals;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
    }
    
    function escapeHTML(str) {
        return str
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // SETTINGS: Load & Save scraper configurations
    function toggleFiltersDisabledState(isRandom) {
        const groupGenres = document.getElementById('group-allowed-genres');
        const groupLanguages = document.getElementById('group-allowed-languages');
        
        if (groupGenres) {
            if (isRandom) groupGenres.classList.add('disabled');
            else groupGenres.classList.remove('disabled');
        }
        if (groupLanguages) {
            if (isRandom) groupLanguages.classList.add('disabled');
            else groupLanguages.classList.remove('disabled');
        }
        
        // Physically disable checkbox elements to block hover states and checks
        document.querySelectorAll('input[name="allowed_genres"]').forEach(chk => {
            chk.disabled = isRandom;
        });
        document.querySelectorAll('input[name="allowed_languages"]').forEach(chk => {
            chk.disabled = isRandom;
        });
    }

    async function loadSettings() {
        try {
            const response = await fetch('/api/config');
            if (!response.ok) throw new Error('Failed to fetch scraper config');
            const data = await response.json();
            
            // Map settings
            const allowedGenres = data.allowed_genres || [];
            const allowedLanguages = data.allowed_languages || [];
            const songsPerRun = data.songs_per_run || 5;
            const filterMode = data.filter_mode || "filtered";
            const isRandom = filterMode === "random";
            
            // Set Filter Mode toggle UI state
            const filterModeToggle = document.getElementById('toggle-filter-mode');
            const optModeFiltered = document.getElementById('opt-mode-filtered');
            const optModeRandom = document.getElementById('opt-mode-random');
            
            if (filterModeToggle) {
                filterModeToggle.checked = isRandom;
            }
            if (isRandom) {
                if (optModeFiltered) optModeFiltered.classList.remove('active');
                if (optModeRandom) optModeRandom.classList.add('active');
            } else {
                if (optModeFiltered) optModeFiltered.classList.add('active');
                if (optModeRandom) optModeRandom.classList.remove('active');
            }
            
            toggleFiltersDisabledState(isRandom);
            
            // Check correct checkboxes
            document.querySelectorAll('input[name="allowed_genres"]').forEach(chk => {
                chk.checked = allowedGenres.includes(chk.value);
            });
            document.querySelectorAll('input[name="allowed_languages"]').forEach(chk => {
                chk.checked = allowedLanguages.includes(chk.value);
            });
            
            const songsInput = document.getElementById('input-songs-per-run');
            if (songsInput) songsInput.value = songsPerRun;
            
            // Map state metadata
            const stateData = data.state || {};
            const settingsCursor = document.getElementById('settings-cursor');
            const settingsPoolSize = document.getElementById('settings-pool-size');
            
            if (settingsCursor) settingsCursor.textContent = stateData.cursor !== undefined ? stateData.cursor : '0';
            if (settingsPoolSize) settingsPoolSize.textContent = stateData.pool_size !== undefined ? stateData.pool_size : '0';
            
        } catch (err) {
            showToast(`Error loading settings: ${err.message}`, 'error');
        }
    }

    async function saveSettings() {
        const btnSave = document.getElementById('btn-save-settings');
        if (btnSave) btnSave.disabled = true;
        
        try {
            const allowedGenres = [];
            document.querySelectorAll('input[name="allowed_genres"]:checked').forEach(chk => {
                allowedGenres.push(chk.value);
            });
            
            const allowedLanguages = [];
            document.querySelectorAll('input[name="allowed_languages"]:checked').forEach(chk => {
                allowedLanguages.push(chk.value);
            });
            
            const songsInput = document.getElementById('input-songs-per-run');
            const songsPerRun = songsInput ? parseInt(songsInput.value) : 5;
            
            const filterModeToggle = document.getElementById('toggle-filter-mode');
            const filterMode = (filterModeToggle && filterModeToggle.checked) ? "random" : "filtered";
            
            const payload = {
                allowed_genres: allowedGenres,
                allowed_languages: allowedLanguages,
                songs_per_run: songsPerRun,
                filter_mode: filterMode
            };
            
            const response = await fetch('/api/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });
            
            if (!response.ok) throw new Error('Failed to save configuration');
            const resData = await response.json();
            
            showToast(resData.message || 'Scraper configuration updated successfully.', 'success');
            
            // Reload settings to refresh values
            loadSettings();
            
        } catch (err) {
            showToast(`Error saving settings: ${err.message}`, 'error');
        } finally {
            if (btnSave) btnSave.disabled = false;
        }
    }

    async function forceRefreshPool() {
        const btnRefresh = document.getElementById('btn-force-refresh-pool');
        if (btnRefresh) btnRefresh.disabled = true;
        
        try {
            const response = await fetch('/api/pool/refresh', {
                method: 'POST'
            });
            if (!response.ok) throw new Error('Failed to force pool refresh');
            const resData = await response.json();
            
            showToast(resData.message || 'Pool expiration set successfully.', 'success');
            
            // Reload settings to get updated state metadata
            loadSettings();
        } catch (err) {
            showToast(`Error refreshing pool: ${err.message}`, 'error');
        } finally {
            if (btnRefresh) btnRefresh.disabled = false;
        }
    }

    // Toggle switch handler logic
    const filterModeToggle = document.getElementById('toggle-filter-mode');
    const optModeFiltered = document.getElementById('opt-mode-filtered');
    const optModeRandom = document.getElementById('opt-mode-random');
    
    if (filterModeToggle) {
        filterModeToggle.addEventListener('change', () => {
            const isRandom = filterModeToggle.checked;
            if (isRandom) {
                if (optModeFiltered) optModeFiltered.classList.remove('active');
                if (optModeRandom) optModeRandom.classList.add('active');
            } else {
                if (optModeFiltered) optModeFiltered.classList.add('active');
                if (optModeRandom) optModeRandom.classList.remove('active');
            }
            toggleFiltersDisabledState(isRandom);
        });
    }

    const btnSaveSettings = document.getElementById('btn-save-settings');
    if (btnSaveSettings) {
        btnSaveSettings.addEventListener('click', saveSettings);
    }
    
    const btnForceRefreshPool = document.getElementById('btn-force-refresh-pool');
    if (btnForceRefreshPool) {
        btnForceRefreshPool.addEventListener('click', forceRefreshPool);
    }

    const btnNormalizeDb = document.getElementById('btn-normalize-db');
    if (btnNormalizeDb) {
        btnNormalizeDb.addEventListener('click', async () => {
            btnNormalizeDb.disabled = true;
            showToast('Starting database normalization...', 'info');
            try {
                const response = await fetch('/api/library/normalize', { method: 'POST' });
                const resData = await response.json();
                if (response.ok && resData.status === 'success') {
                    showToast(resData.message || 'Database normalized successfully.', 'success');
                    loadStorage();
                    loadTracks(false);
                } else {
                    throw new Error(resData.error || 'Failed to normalize database');
                }
            } catch (err) {
                showToast(`Normalization failed: ${err.message}`, 'error');
            } finally {
                btnNormalizeDb.disabled = false;
            }
        });
    }

    const btnAuditLibrary = document.getElementById('btn-audit-library');
    if (btnAuditLibrary) {
        btnAuditLibrary.addEventListener('click', async () => {
            btnAuditLibrary.disabled = true;
            btnAuditLibrary.textContent = 'Auditing...';
            showToast('Auditing database fields...', 'info');
            try {
                const response = await fetch('/api/library/audit');
                const resData = await response.json();
                if (response.ok && resData.status === 'success') {
                    showToast('Audit complete.', 'success');
                    const d = resData.data;
                    const m = d.missing_counts;
                    let msg = `Library Audit Results\n\n`;
                    msg += `Total Tracks: ${d.total_tracks}\n`;
                    msg += `Complete Tracks: ${d.complete_tracks}\n`;
                    msg += `Incomplete Tracks: ${d.tracks_with_any_missing_field}\n\n`;
                    msg += `Missing Field Counts:\n`;
                    msg += `- Album Art: ${m.album_art}\n`;
                    msg += `- Duration: ${m.duration}\n`;
                    msg += `- Duration Seconds: ${m.durationSeconds}\n`;
                    msg += `- Language: ${m.language}\n`;
                    msg += `- Genre: ${m.genre}\n`;
                    msg += `- Album: ${m.album}\n`;
                    msg += `- Lyrics: ${m.lyrics}\n`;
                    msg += `- Synced Lyrics: ${m.syncedLyrics}\n`;
                    msg += `- Source: ${m.source}\n`;
                    msg += `- Spotify ID (Info only): ${m.spotify_id}\n`;
                    alert(msg);
                } else {
                    throw new Error(resData.error || 'Failed to audit database');
                }
            } catch (err) {
                showToast(`Audit failed: ${err.message}`, 'error');
            } finally {
                btnAuditLibrary.disabled = false;
                btnAuditLibrary.textContent = 'Audit Library';
            }
        });
    }

    const btnRunCompleteBackfill = document.getElementById('btn-run-complete-backfill');
    if (btnRunCompleteBackfill) {
        btnRunCompleteBackfill.addEventListener('click', async () => {
            btnRunCompleteBackfill.disabled = true;
            try {
                const response = await fetch('/api/backfill/complete', { method: 'POST' });
                const resData = await response.json();
                if (response.ok && resData.status === 'success') {
                    showToast('Complete Backfill Engine started in background.', 'success');
                    setTimeout(pollBackgroundStatus, 500);
                } else {
                    throw new Error(resData.error || 'Failed to start backfill engine');
                }
            } catch (err) {
                showToast(`Error: ${err.message}`, 'error');
                btnRunCompleteBackfill.disabled = false;
            }
        });
    }

    const btnRunGeminiBackfill = document.getElementById('btn-run-gemini-backfill');
    if (btnRunGeminiBackfill) {
        btnRunGeminiBackfill.addEventListener('click', async () => {
            btnRunGeminiBackfill.disabled = true;
            try {
                const response = await fetch('/api/backfill/gemini', { method: 'POST' });
                const resData = await response.json();
                if (response.ok && resData.status === 'success') {
                    showToast('Gemini AI Backfill Engine started in background.', 'success');
                    setTimeout(pollBackgroundStatus, 500);
                } else {
                    throw new Error(resData.error || 'Failed to start Gemini backfill');
                }
            } catch (err) {
                showToast(`Error: ${err.message}`, 'error');
                btnRunGeminiBackfill.disabled = false;
            }
        });
    }

    // ADD SONG: Modal Handlers
    const addSongModal = document.getElementById('add-song-modal');
    const btnOpenAddSongModal = document.getElementById('btn-open-add-song-modal');
    const addSongModalCloseX = document.getElementById('add-song-modal-close-x');
    const addSongModalBtnCancel = document.getElementById('add-song-modal-btn-cancel');
    
    const inputSpotifyUrl = document.getElementById('input-spotify-url');
    const btnPreviewSong = document.getElementById('btn-preview-song');
    const btnConfirmAddSong = document.getElementById('btn-confirm-add-song');
    
    const songPreviewContainer = document.getElementById('song-preview-container');
    const songPreviewArtwork = document.getElementById('song-preview-artwork');
    const songPreviewTitle = document.getElementById('song-preview-title');
    const songPreviewArtist = document.getElementById('song-preview-artist');
    const songPreviewLanguage = document.getElementById('song-preview-language');
    const songPreviewGenre = document.getElementById('song-preview-genre');
    
    const songProgressContainer = document.getElementById('song-progress-container');
    const songProgressText = document.getElementById('song-progress-text');
    const songStatusMessage = document.getElementById('song-status-message');

    function openAddSongModal() {
        if (addSongModal) {
            // Reset modal inputs and states
            if (inputSpotifyUrl) inputSpotifyUrl.value = '';
            hideAddSongPreview();
            hideAddSongProgress();
            hideAddSongStatus();
            if (btnConfirmAddSong) {
                btnConfirmAddSong.disabled = true;
                btnConfirmAddSong.classList.add('disabled');
            }
            addSongModal.classList.remove('hidden');
        }
    }

    function closeAddSongModal() {
        if (addSongModal) {
            addSongModal.classList.add('hidden');
        }
    }

    function hideAddSongPreview() {
        if (songPreviewContainer) songPreviewContainer.classList.add('hidden');
    }
    function hideAddSongProgress() {
        if (songProgressContainer) songProgressContainer.classList.add('hidden');
    }
    function hideAddSongStatus() {
        if (songStatusMessage) {
            songStatusMessage.classList.add('hidden');
            songStatusMessage.innerHTML = '';
            songStatusMessage.className = 'song-status-message';
        }
    }

    if (btnOpenAddSongModal) btnOpenAddSongModal.addEventListener('click', openAddSongModal);
    [addSongModalCloseX, addSongModalBtnCancel].forEach(el => {
        if (el) el.addEventListener('click', closeAddSongModal);
    });

    // Close modal if clicked outside card
    window.addEventListener('click', (e) => {
        if (e.target === addSongModal) {
            closeAddSongModal();
        }
    });

    async function previewSong() {
        const url = inputSpotifyUrl ? inputSpotifyUrl.value.trim() : '';
        if (!url) {
            showAddSongStatus("Please enter a Spotify track link first.", "error");
            return;
        }

        hideAddSongPreview();
        hideAddSongStatus();
        
        // Show loading progress in modal
        if (songProgressContainer) {
            songProgressContainer.classList.remove('hidden');
            songProgressText.textContent = "Retrieving track metadata from Spotify...";
        }
        if (btnPreviewSong) btnPreviewSong.disabled = true;
        if (btnConfirmAddSong) {
            btnConfirmAddSong.disabled = true;
            btnConfirmAddSong.classList.add('disabled');
        }

        try {
            const response = await fetch(`/api/preview-song?url=${encodeURIComponent(url)}`);
            const data = await response.json();
            
            if (!response.ok) {
                throw new Error(data.error || "Failed to retrieve song details.");
            }

            // Update Preview fields
            if (songPreviewTitle) songPreviewTitle.textContent = data.title || "Unknown Title";
            if (songPreviewArtist) songPreviewArtist.textContent = data.artist || "Unknown Artist";
            if (songPreviewLanguage) songPreviewLanguage.textContent = data.language || "unknown";
            if (songPreviewGenre) songPreviewGenre.textContent = data.genre || "Unknown";
            
            if (songPreviewArtwork) {
                if (data.album_art) {
                    songPreviewArtwork.src = data.album_art;
                    songPreviewArtwork.classList.remove('hidden');
                } else {
                    songPreviewArtwork.src = "";
                    songPreviewArtwork.classList.add('hidden');
                }
            }

            // Show preview
            if (songPreviewContainer) songPreviewContainer.classList.remove('hidden');
            
            // Enable Add Button
            if (btnConfirmAddSong) {
                btnConfirmAddSong.disabled = false;
                btnConfirmAddSong.classList.remove('disabled');
            }
        } catch (err) {
            showAddSongStatus(err.message, "error");
        } finally {
            hideAddSongProgress();
            if (btnPreviewSong) btnPreviewSong.disabled = false;
        }
    }

    async function addSong() {
        const url = inputSpotifyUrl ? inputSpotifyUrl.value.trim() : '';
        if (!url) return;

        hideAddSongStatus();
        
        // Show progress in modal
        if (songProgressContainer) {
            songProgressContainer.classList.remove('hidden');
            songProgressText.textContent = "Requesting single song import...";
        }
        
        // Disable action buttons in modal during processing
        if (btnPreviewSong) btnPreviewSong.disabled = true;
        if (btnConfirmAddSong) {
            btnConfirmAddSong.disabled = true;
            btnConfirmAddSong.classList.add('disabled');
        }
        if (addSongModalBtnCancel) addSongModalBtnCancel.disabled = true;

        try {
            const response = await fetch('/api/add-song', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ spotify_url: url })
            });
            const data = await response.json();
            
            if (!response.ok) {
                throw new Error(data.error || "Failed to add song.");
            }

            showToast("Song import started in background.", "info");
            singleAddRunning = true;
            
            // Trigger background poller immediately to update UI
            setTimeout(pollBackgroundStatus, 500);
        } catch (err) {
            showAddSongStatus(`Error: ${err.message}`, "error");
            hideAddSongProgress();
            if (btnPreviewSong) btnPreviewSong.disabled = false;
            if (addSongModalBtnCancel) addSongModalBtnCancel.disabled = false;
        }
    }

    function showAddSongStatus(message, type) {
        if (songStatusMessage) {
            songStatusMessage.textContent = message;
            songStatusMessage.className = `song-status-message ${type}`;
            songStatusMessage.classList.remove('hidden');
        }
    }

    if (btnPreviewSong) btnPreviewSong.addEventListener('click', previewSong);
    if (btnConfirmAddSong) btnConfirmAddSong.addEventListener('click', addSong);

    // === Playlist Importer Logic ===
    const playlistUrlInputs = Array.from(document.querySelectorAll('.playlist-url-input'));
    const btnPreviewPlaylist = document.getElementById('btn-preview-playlist');
    const playlistPreviewContainer = document.getElementById('playlist-preview-container');
    const previewPlaylistName = document.getElementById('preview-playlist-name');
    const previewPlaylistCount = document.getElementById('preview-playlist-count');
    const previewPlaylistNewTracks = document.getElementById('preview-playlist-new-tracks');
    const previewPlaylistAlreadyInLibrary = document.getElementById('preview-playlist-already-in-library');
    const previewPlaylistSize = document.getElementById('preview-playlist-size');
    const btnCancelPlaylistPreview = document.getElementById('btn-cancel-playlist-preview');
    const btnStartPlaylistImport = document.getElementById('btn-start-playlist-import');
    const previewTracksList = document.getElementById('preview-tracks-list');
    
    const playlistProgressContainer = document.getElementById('playlist-progress-container');
    const playlistStatusBadge = document.getElementById('playlist-status-badge');
    const playlistProgressText = document.getElementById('playlist-progress-text');
    const playlistStatDownloaded = document.getElementById('playlist-stat-downloaded');
    const playlistStatSkipped = document.getElementById('playlist-stat-skipped');
    const playlistStatFailed = document.getElementById('playlist-stat-failed');
    const btnCancelPlaylistImport = document.getElementById('btn-cancel-playlist-import');
    const playlistProgressFill = document.getElementById('playlist-progress-fill');
    const playlistQueueProgress = document.getElementById('playlist-queue-progress');
    
    let currentPlaylistId = null;
    let currentPlaylistQueueId = null;
    let playlistPreviewResults = [];
    let playlistBoxOrderByUrl = new Map();
    const maxPlaylistUrlBoxes = 5;

    const spotifyPlaylistPattern = /(?:https?:\/\/open\.spotify\.com\/(?:intl-[a-z]{2}\/)?playlist\/[A-Za-z0-9]+[^\s<>"']*|spotify:playlist:[A-Za-z0-9]+)/gi;

    function normalizePlaylistUrl(value) {
        const cleaned = (value || '').trim().replace(/^['"(<\[{]+|['").,;>\]}]+$/g, '');
        if (!cleaned) return null;
        const idMatch = cleaned.match(/(?:playlist\/|spotify:playlist:)([A-Za-z0-9]+)/i);
        if (idMatch) return `https://open.spotify.com/playlist/${idMatch[1]}`;
        if (/^[A-Za-z0-9]{16,32}$/.test(cleaned)) return `https://open.spotify.com/playlist/${cleaned}`;
        return null;
    }

    function parsePlaylistUrlsFromText(raw) {
        const matches = raw.match(spotifyPlaylistPattern);
        const candidates = matches && matches.length ? matches : raw.split(/[\s,\n\r]+/);
        const seen = new Set();
        const urls = [];

        candidates.forEach(candidate => {
            const normalized = normalizePlaylistUrl(candidate);
            if (normalized && !seen.has(normalized)) {
                seen.add(normalized);
                urls.push(normalized);
            }
        });

        return urls;
    }

    function getPlaylistUrlsFromInput() {
        return getPlaylistEntriesFromInput().map(entry => entry.url);
    }

    function getPlaylistEntriesFromInput() {
        const seen = new Set();
        const entries = [];

        playlistUrlInputs.forEach((input, index) => {
            parsePlaylistUrlsFromText(input.value || '').forEach(url => {
                if (!seen.has(url) && entries.length < maxPlaylistUrlBoxes) {
                    seen.add(url);
                    entries.push({
                        url,
                        boxNumber: index + 1
                    });
                }
            });
        });

        return entries;
    }

    function setPlaylistInputUrls(urls) {
        playlistUrlInputs.forEach((input, index) => {
            input.value = urls[index] || '';
        });
        playlistPreviewResults = [];
        playlistBoxOrderByUrl = new Map();
        if (playlistPreviewContainer) playlistPreviewContainer.classList.add('hidden');
    }

    function rememberPlaylistBoxOrder(entries) {
        playlistBoxOrderByUrl = new Map(entries.map(entry => [entry.url, entry.boxNumber]));
    }

    function handlePlaylistUrlPaste(event) {
        const pasted = event.clipboardData?.getData('text') || '';
        const pastedUrls = parsePlaylistUrlsFromText(pasted);
        if (pastedUrls.length <= 1) return;

        event.preventDefault();
        const startIndex = playlistUrlInputs.indexOf(event.currentTarget);
        const values = playlistUrlInputs.map(input => input.value.trim());
        let cursor = startIndex >= 0 ? startIndex : 0;

        pastedUrls.forEach(url => {
            if (cursor < maxPlaylistUrlBoxes) {
                values[cursor] = url;
                cursor += 1;
            }
        });

        setPlaylistInputUrls(values);
        if (pastedUrls.length > maxPlaylistUrlBoxes - startIndex) {
            showToast(`Only ${maxPlaylistUrlBoxes} playlist boxes are available, so extra links were ignored.`, 'info');
        }
    }

    function playlistStatusLabel(status) {
        const labels = {
            queued: 'Queued',
            starting: 'Starting',
            running: 'Running',
            completed: 'Completed',
            completed_with_errors: 'Completed with errors',
            failed: 'Failed',
            error: 'Error',
            cancelled: 'Cancelled'
        };
        return labels[status] || 'Queued';
    }

    function safePlaylistStatusClass(status) {
        return ['queued', 'starting', 'running', 'completed', 'completed_with_errors', 'failed', 'error', 'cancelled'].includes(status) ? status : 'queued';
    }

    function renderPlaylistPreviewItems(items) {
        if (!previewTracksList) return;
        if (!items.length) {
            previewTracksList.innerHTML = '<div class="playlist-preview-item"><div class="playlist-preview-item-main"><div class="playlist-preview-item-title">No valid playlist links found</div></div></div>';
            return;
        }

        previewTracksList.innerHTML = items.map(item => {
            const status = item.status === 'error' ? 'error' : 'queued';
            const title = item.status === 'error' ? 'Preview failed' : (item.playlist_name || 'Spotify Playlist');
            const boxLabel = item.boxNumber ? `Box ${item.boxNumber}` : `Link ${item.index + 1}`;
            const tracks = item.tracks_available_for_import ?? item.total_tracks ?? 0;
            const dupCount = item.already_in_library || 0;
            const newCount = item.new_tracks_importable !== undefined ? item.new_tracks_importable : tracks;
            const dupMeta = dupCount > 0 ? ` (${newCount} new, ${dupCount} in library)` : '';
            const sampleTracks = (item.preview_tracks || []).slice(0, 3)
                .map(track => `${escapeHtml(track.title || 'Unknown')} - ${escapeHtml(track.artist || 'Unknown')}`)
                .join('; ');
            const meta = item.status === 'error'
                ? escapeHtml(item.error || 'Unable to preview this playlist')
                : `${tracks} importable tracks${dupMeta} | ${escapeHtml(item.estimated_size_display || '~0 MB')}${sampleTracks ? ` | ${sampleTracks}` : ''}`;
            const source = item.url ? `<div class="playlist-preview-item-meta">${escapeHtml(item.url)}</div>` : '';
            const warning = item.truncated && item.truncation_warning
                ? `<div class="playlist-preview-item-meta" style="color: #ffbd2e; margin-top: 4px;">${escapeHtml(item.truncation_warning)}</div>`
                : '';

            return `
                <div class="playlist-preview-item">
                    <div class="playlist-preview-item-main">
                        <div class="playlist-preview-item-title">${escapeHtml(boxLabel)}: ${escapeHtml(title)}</div>
                        ${source}
                        <div class="playlist-preview-item-meta">${meta}</div>
                        ${warning}
                    </div>
                    <span class="playlist-queue-status ${safePlaylistStatusClass(status)}">${item.status === 'error' ? 'Error' : 'Ready'}</span>
                </div>
            `;
        }).join('');
    }

    function renderPlaylistQueueProgress(queue = [], currentIndex = null, queueStatus = 'idle') {
        if (!playlistQueueProgress) return;
        if (!queue.length) {
            playlistQueueProgress.innerHTML = '';
            return;
        }

        playlistQueueProgress.innerHTML = queue.map((item, index) => {
            const status = item.status || 'queued';
            const activeText = queueStatus === 'running' && index === currentIndex
                ? 'Current'
                : playlistStatusLabel(status);
            const title = item.playlist_name || `Playlist ${index + 1}`;
            const boxNumber = item.boxNumber || playlistBoxOrderByUrl.get(item.url);
            const titlePrefix = boxNumber ? `Box ${boxNumber}: ` : `${index + 1}. `;
            const processed = item.processed || 0;
            const total = item.total_tracks || 0;
            const progressText = total > 0 ? `${processed} / ${total} tracks` : (status === 'queued' ? 'Waiting' : 'Preparing');
            const counts = `Downloaded ${item.downloaded || 0} | Skipped ${item.skipped || 0} | Failed ${item.failed || 0}`;
            const error = item.error ? `<div class="playlist-queue-item-meta" style="color: #ff453a;">${escapeHtml(item.error)}</div>` : '';

            return `
                <div class="playlist-queue-item">
                    <div class="playlist-queue-item-main">
                        <div class="playlist-queue-item-title">${escapeHtml(titlePrefix)}${escapeHtml(title)}</div>
                        <div class="playlist-queue-item-meta">${escapeHtml(progressText)} | ${escapeHtml(counts)}</div>
                        ${error}
                    </div>
                    <span class="playlist-queue-status ${safePlaylistStatusClass(status)}">${escapeHtml(activeText)}</span>
                </div>
            `;
        }).join('');
    }

    playlistUrlInputs.forEach(input => {
        input.addEventListener('input', () => {
            playlistPreviewResults = [];
            if (playlistPreviewContainer) playlistPreviewContainer.classList.add('hidden');
        });
        input.addEventListener('paste', handlePlaylistUrlPaste);
        input.addEventListener('blur', () => {
            const normalized = normalizePlaylistUrl(input.value);
            if (normalized) input.value = normalized;
        });
    });

    if (btnPreviewPlaylist) {
        btnPreviewPlaylist.addEventListener('click', async () => {
            const playlistEntries = getPlaylistEntriesFromInput();
            const urls = playlistEntries.map(entry => entry.url);
            if (!urls.length) return showToast("Please enter at least one playlist URL", "error");
            rememberPlaylistBoxOrder(playlistEntries);
            
            btnPreviewPlaylist.disabled = true;
            btnPreviewPlaylist.textContent = "Loading...";
            
            try {
                const response = await fetch('/api/playlist/queue/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({urls})
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || "Failed to preview playlists");
                
                const previewPlaylistWarning = document.getElementById('playlist-preview-warning');
                playlistPreviewResults = (data.previews || []).map((item, index) => ({
                    ...item,
                    boxNumber: playlistEntries[index]?.boxNumber || index + 1
                }));
                const readyItems = playlistPreviewResults.filter(item => item.status !== 'error');
                const truncatedCount = readyItems.filter(item => item.truncated).length;
                const warningParts = [];
                if (data.error_count) warningParts.push(`${data.error_count} link${data.error_count === 1 ? '' : 's'} could not be previewed and will be skipped.`);
                if (truncatedCount) warningParts.push(`${truncatedCount} playlist${truncatedCount === 1 ? '' : 's'} can only import the visible Spotify embed tracks.`);

                if (previewPlaylistWarning && warningParts.length) {
                    previewPlaylistWarning.textContent = warningParts.join(' ');
                    previewPlaylistWarning.style.display = 'block';
                } else if (previewPlaylistWarning) {
                    previewPlaylistWarning.style.display = 'none';
                }

                if (readyItems.length === 1) {
                    const item = readyItems[0];
                    previewPlaylistName.textContent = item.playlist_name || 'Spotify Playlist';
                    previewPlaylistCount.textContent = `${item.tracks_available_for_import || item.total_tracks || 0} Tracks`;
                    const newCount = item.new_tracks_importable !== undefined ? item.new_tracks_importable : (item.tracks_available_for_import || item.total_tracks || 0);
                    const alreadyCount = item.already_in_library || 0;
                    if (previewPlaylistNewTracks) {
                        previewPlaylistNewTracks.textContent = `${newCount} New tracks to import`;
                    }
                    if (previewPlaylistAlreadyInLibrary) {
                        previewPlaylistAlreadyInLibrary.textContent = `${alreadyCount} Already in library`;
                    }
                    previewPlaylistSize.textContent = item.estimated_size_display || '~0 MB';
                } else {
                    const totalTracks = data.total_tracks || 0;
                    const totalAlready = data.total_already_in_library !== undefined
                        ? data.total_already_in_library
                        : readyItems.reduce((sum, item) => sum + (item.already_in_library || 0), 0);
                    const totalNew = data.total_new_tracks_importable !== undefined
                        ? data.total_new_tracks_importable
                        : readyItems.reduce((sum, item) => sum + (item.new_tracks_importable !== undefined ? item.new_tracks_importable : (item.tracks_available_for_import || item.total_tracks || 0)), 0);

                    previewPlaylistName.textContent = `${readyItems.length} Playlists Ready`;
                    previewPlaylistCount.textContent = `${totalTracks} Importable Tracks`;
                    if (previewPlaylistNewTracks) {
                        previewPlaylistNewTracks.textContent = `${totalNew} New tracks to import`;
                    }
                    if (previewPlaylistAlreadyInLibrary) {
                        previewPlaylistAlreadyInLibrary.textContent = `${totalAlready} Already in library`;
                    }
                    previewPlaylistSize.textContent = data.estimated_size_display || '~0 MB';
                }
                
                renderPlaylistPreviewItems(playlistPreviewResults);
                if (btnStartPlaylistImport) btnStartPlaylistImport.disabled = readyItems.length === 0;
                
                playlistPreviewContainer.classList.remove('hidden');
                playlistProgressContainer.classList.add('hidden');
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                btnPreviewPlaylist.disabled = false;
                btnPreviewPlaylist.textContent = "Preview Playlists";
            }
        });
    }

    if (btnCancelPlaylistPreview) {
        btnCancelPlaylistPreview.addEventListener('click', () => {
            playlistPreviewContainer.classList.add('hidden');
            setPlaylistInputUrls([]);
        });
    }

    if (btnStartPlaylistImport) {
        btnStartPlaylistImport.addEventListener('click', async () => {
            const playlistEntries = getPlaylistEntriesFromInput();
            const previewUrls = playlistPreviewResults
                .filter(item => item.status !== 'error')
                .map(item => item.url);
            const urls = previewUrls.length ? previewUrls : playlistEntries.map(entry => entry.url);
            if (!urls.length) return showToast("No valid playlist URLs are ready to import", "error");
            rememberPlaylistBoxOrder(
                previewUrls.length
                    ? playlistPreviewResults.filter(item => item.status !== 'error').map(item => ({
                        url: item.url,
                        boxNumber: item.boxNumber
                    }))
                    : playlistEntries
            );

            btnStartPlaylistImport.disabled = true;
            
            try {
                const response = await fetch('/api/playlist/queue/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({urls})
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || "Failed to start import");
                
                currentPlaylistId = data.playlist_id;
                currentPlaylistQueueId = data.queue_id;
                playlistPreviewContainer.classList.add('hidden');
                playlistProgressContainer.classList.remove('hidden');
                
                // reset progress
                playlistProgressFill.style.width = '0%';
                playlistProgressText.textContent = `Queue started: 0 / ${data.queue_total || urls.length} playlists`;
                playlistStatDownloaded.innerHTML = `<span class="stat-number">0</span><span class="stat-label">Downloaded</span>`;
                playlistStatSkipped.innerHTML = `<span class="stat-number">0</span><span class="stat-label">Skipped</span>`;
                playlistStatFailed.innerHTML = `<span class="stat-number">0</span><span class="stat-label">Failed</span>`;
                playlistStatusBadge.textContent = "Running";
                playlistStatusBadge.style.background = "rgba(255,255,255,0.1)";
                renderPlaylistQueueProgress(data.queue || urls.map((url, index) => ({ index, url, status: 'queued' })));
                
                showToast(`${urls.length} playlist${urls.length === 1 ? '' : 's'} queued for import`, "success");
                setTimeout(pollBackgroundStatus, 500);
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                if (!playlistPreviewResults.length || playlistPreviewResults.some(item => item.status !== 'error')) {
                    btnStartPlaylistImport.disabled = false;
                }
            }
        });
    }

    if (btnCancelPlaylistImport) {
        btnCancelPlaylistImport.addEventListener('click', async () => {
            if (!confirm('Cancel this import? Any track currently downloading will stop immediately and any queued tracks will be skipped.')) return;
            try {
                btnCancelPlaylistImport.disabled = true;
                const response = await fetch('/api/playlist/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        playlist_id: currentPlaylistId,
                        queue_id: currentPlaylistQueueId,
                        cancel_queue: true
                    })
                });
                if (!response.ok) {
                    const data = await response.json().catch(() => ({}));
                    throw new Error(data.error || "Failed to cancel import");
                }
                playlistStatusBadge.textContent = "Cancelled";
                playlistStatusBadge.style.background = "#ff453a";
                showToast("Playlist queue cancellation requested", "success");
                setTimeout(pollBackgroundStatus, 500);
            } catch (err) {
                showToast(err.message, "error");
                btnCancelPlaylistImport.disabled = false;
            }
        });
    }

    // === Spotify Library Importer Logic ===
    const spotifyLibraryUrlInput = document.getElementById('spotify-library-url-input');
    const spotifyLibraryConnectionStatus = document.getElementById('spotify-library-connection-status');
    const btnConnectSpotifyLibrary = document.getElementById('btn-connect-spotify-library');
    const btnDiagnoseSpotifyLibrary = document.getElementById('btn-diagnose-spotify-library');
    const btnPreviewSpotifyLibrary = document.getElementById('btn-preview-spotify-library');
    const spotifyLibraryPreviewContainer = document.getElementById('spotify-library-preview-container');
    const spotifyLibraryPreviewWarning = document.getElementById('spotify-library-preview-warning');
    const spotifyLibraryPreviewName = document.getElementById('spotify-library-preview-name');
    const spotifyLibraryPreviewCount = document.getElementById('spotify-library-preview-count');
    const spotifyLibraryPreviewNewTracks = document.getElementById('spotify-library-preview-new-tracks');
    const spotifyLibraryPreviewAlreadyInLibrary = document.getElementById('spotify-library-preview-already-in-library');
    const spotifyLibraryPreviewSize = document.getElementById('spotify-library-preview-size');
    const spotifyLibraryPreviewTracksList = document.getElementById('spotify-library-preview-tracks-list');
    const btnCancelSpotifyLibraryPreview = document.getElementById('btn-cancel-spotify-library-preview');
    const btnStartSpotifyLibraryImport = document.getElementById('btn-start-spotify-library-import');
    const spotifyLibraryProgressContainer = document.getElementById('spotify-library-progress-container');
    const spotifyLibraryStatusBadge = document.getElementById('spotify-library-status-badge');
    const spotifyLibraryProgressText = document.getElementById('spotify-library-progress-text');
    const spotifyLibraryProgressFill = document.getElementById('spotify-library-progress-fill');
    const spotifyLibraryStatDownloaded = document.getElementById('spotify-library-stat-downloaded');
    const spotifyLibraryStatSkipped = document.getElementById('spotify-library-stat-skipped');
    const spotifyLibraryStatFailed = document.getElementById('spotify-library-stat-failed');
    const spotifyLibraryGeminiStatus = document.getElementById('spotify-library-gemini-status');
    const btnCancelSpotifyLibraryImport = document.getElementById('btn-cancel-spotify-library-import');
    let spotifyLibraryPreviewResult = null;
    let currentSpotifyLibraryPlaylistId = null;
    let currentSpotifyLibraryTaskId = null;
    if (btnStartSpotifyLibraryImport) btnStartSpotifyLibraryImport.disabled = true;

    async function readSpotifyLibraryJson(response, fallbackMessage) {
        const contentType = response.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || fallbackMessage);
            return data;
        }

        const text = await response.text();
        const looksLikeMissingRoute = response.status === 404 && text.toLowerCase().includes('<!doctype');
        if (looksLikeMissingRoute) {
            throw new Error('Spotify Library Importer backend route is not loaded yet. Restart the Flask server and refresh this page.');
        }
        const cleaned = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
        throw new Error(cleaned || fallbackMessage);
    }

    function setSpotifyLibraryConnectionStatus(message, type = 'info') {
        if (!spotifyLibraryConnectionStatus) return;
        const colors = {
            success: '#30d158',
            error: '#ff453a',
            warning: '#ffbd2e',
            info: 'var(--tertiary)'
        };
        spotifyLibraryConnectionStatus.textContent = message;
        spotifyLibraryConnectionStatus.style.color = colors[type] || colors.info;
    }

    async function checkSpotifyLibraryConnection() {
        if (!spotifyLibraryConnectionStatus) return;
        try {
            const response = await fetch('/api/spotify-library/connection?check=1');
            const data = await readSpotifyLibraryJson(response, 'Spotify connection check failed');
            if (data.ready) {
                setSpotifyLibraryConnectionStatus('Spotify API connection ready.', 'success');
                if (btnConnectSpotifyLibrary) btnConnectSpotifyLibrary.classList.add('hidden');
            } else {
                setSpotifyLibraryConnectionStatus(data.error || 'Spotify API is not configured yet.', 'warning');
                if (btnConnectSpotifyLibrary) {
                    if (data.auth_url_available && data.missing?.includes('SPOTIFY_REFRESH_TOKEN')) {
                        btnConnectSpotifyLibrary.classList.remove('hidden');
                    } else {
                        btnConnectSpotifyLibrary.classList.add('hidden');
                    }
                }
            }
        } catch (err) {
            setSpotifyLibraryConnectionStatus(err.message, 'error');
            if (btnConnectSpotifyLibrary) btnConnectSpotifyLibrary.classList.add('hidden');
        }
    }

    if (btnConnectSpotifyLibrary) {
        btnConnectSpotifyLibrary.addEventListener('click', async () => {
            btnConnectSpotifyLibrary.disabled = true;
            try {
                const response = await fetch('/api/spotify-library/auth-url');
                const data = await readSpotifyLibraryJson(response, 'Failed to create Spotify authorization URL');
                if (!data.auth_url) throw new Error('Spotify authorization URL was not returned');
                window.open(data.auth_url, '_blank', 'noopener,noreferrer');
                showToast('Spotify authorization page opened. Use the callback result as SPOTIFY_REFRESH_TOKEN.', 'info');
            } catch (err) {
                showToast(err.message, 'error');
            } finally {
                btnConnectSpotifyLibrary.disabled = false;
            }
        });
    }

    function renderSpotifyLibraryPreview(preview) {
        if (!spotifyLibraryPreviewTracksList || !preview) return;
        const tracks = (preview.preview_tracks || []).slice(0, 5);
        spotifyLibraryPreviewTracksList.innerHTML = tracks.length
            ? tracks.map((track, index) => `
                <div class="playlist-preview-item">
                    <div class="playlist-preview-item-main">
                        <div class="playlist-preview-item-title">${index + 1}. ${escapeHtml(track.title || 'Unknown Title')}</div>
                        <div class="playlist-preview-item-meta">${escapeHtml(track.artist || 'Unknown Artist')}</div>
                    </div>
                    <span class="playlist-queue-status completed">API</span>
                </div>
            `).join('')
            : '<div class="playlist-preview-item"><div class="playlist-preview-item-main"><div class="playlist-preview-item-title">No preview tracks returned</div></div></div>';
    }

    function summarizeSpotifyDiagnosis(data) {
        const account = data.me?.display_name || data.me?.id || 'unknown account';
        const metadata = data.playlist_metadata || {};
        const tracks = data.playlist_tracks || {};
        const userPlaylists = data.user_playlists || {};
        const parts = [
            `Token account: ${account}`,
            `Playlist metadata: HTTP ${metadata.status_code || 'n/a'}${metadata.message ? ` (${metadata.message})` : ''}`,
            `Playlist tracks: HTTP ${tracks.status_code || 'n/a'}${tracks.message ? ` (${tracks.message})` : ''}`,
            `Visible in your first ${userPlaylists.sample_playlist_count ?? 0} library playlists: ${data.visible_in_user_playlists === true ? 'yes' : data.visible_in_user_playlists === false ? 'no' : 'unknown'}`
        ];
        if (metadata.name) parts.push(`Playlist name: ${metadata.name}`);
        return parts.join(' | ');
    }

    if (spotifyLibraryUrlInput) {
        spotifyLibraryUrlInput.addEventListener('input', () => {
            spotifyLibraryPreviewResult = null;
            if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.add('hidden');
        });
        spotifyLibraryUrlInput.addEventListener('blur', () => {
            const normalized = normalizePlaylistUrl(spotifyLibraryUrlInput.value);
            if (normalized) spotifyLibraryUrlInput.value = normalized;
        });
    }

    if (btnPreviewSpotifyLibrary) {
        btnPreviewSpotifyLibrary.addEventListener('click', async () => {
            const url = normalizePlaylistUrl(spotifyLibraryUrlInput?.value);
            if (!url) return showToast('Please enter one Spotify playlist URL', 'error');

            btnPreviewSpotifyLibrary.disabled = true;
            btnPreviewSpotifyLibrary.textContent = 'Loading...';
            spotifyLibraryPreviewResult = null;
            if (btnStartSpotifyLibraryImport) btnStartSpotifyLibraryImport.disabled = true;
            try {
                const response = await fetch('/api/spotify-library/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url})
                });
                const data = await readSpotifyLibraryJson(response, 'Failed to preview Spotify library playlist');

                spotifyLibraryPreviewResult = data;
                if (spotifyLibraryPreviewName) spotifyLibraryPreviewName.textContent = data.playlist_name || 'Spotify Library Playlist';
                if (spotifyLibraryPreviewCount) spotifyLibraryPreviewCount.textContent = `${data.tracks_available_for_import || data.total_tracks || 0} Tracks`;
                if (spotifyLibraryPreviewNewTracks) {
                    const newCount = data.new_tracks_importable !== undefined ? data.new_tracks_importable : (data.tracks_available_for_import || data.total_tracks || 0);
                    spotifyLibraryPreviewNewTracks.textContent = `${newCount} New tracks to import`;
                }
                if (spotifyLibraryPreviewAlreadyInLibrary) {
                    spotifyLibraryPreviewAlreadyInLibrary.textContent = `${data.already_in_library || 0} Already in library`;
                }
                if (spotifyLibraryPreviewSize) spotifyLibraryPreviewSize.textContent = data.estimated_size_display || '~0 MB';
                if (spotifyLibraryPreviewWarning) {
                    spotifyLibraryPreviewWarning.style.display = 'none';
                    spotifyLibraryPreviewWarning.textContent = '';
                }
                renderSpotifyLibraryPreview(data);
                if (btnStartSpotifyLibraryImport) btnStartSpotifyLibraryImport.disabled = false;
                if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.remove('hidden');
                if (spotifyLibraryProgressContainer) spotifyLibraryProgressContainer.classList.add('hidden');
                showToast('Spotify library playlist preview ready', 'success');
            } catch (err) {
                if (spotifyLibraryPreviewWarning) {
                    spotifyLibraryPreviewWarning.textContent = err.message;
                    spotifyLibraryPreviewWarning.style.display = 'block';
                    if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.remove('hidden');
                }
                showToast(err.message, 'error');
            } finally {
                btnPreviewSpotifyLibrary.disabled = false;
                btnPreviewSpotifyLibrary.textContent = 'Preview Library Playlist';
            }
        });
    }

    if (btnDiagnoseSpotifyLibrary) {
        btnDiagnoseSpotifyLibrary.addEventListener('click', async () => {
            const url = normalizePlaylistUrl(spotifyLibraryUrlInput?.value);
            if (!url) return showToast('Please enter one Spotify playlist URL', 'error');

            btnDiagnoseSpotifyLibrary.disabled = true;
            btnDiagnoseSpotifyLibrary.textContent = 'Checking...';
            try {
                const response = await fetch('/api/spotify-library/diagnose', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url})
                });
                const data = await readSpotifyLibraryJson(response, 'Failed to diagnose Spotify access');
                const summary = summarizeSpotifyDiagnosis(data);
                if (spotifyLibraryPreviewWarning) {
                    spotifyLibraryPreviewWarning.textContent = summary;
                    spotifyLibraryPreviewWarning.style.display = 'block';
                }
                if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.remove('hidden');
                showToast('Spotify access diagnosis complete', 'info');
            } catch (err) {
                if (spotifyLibraryPreviewWarning) {
                    spotifyLibraryPreviewWarning.textContent = err.message;
                    spotifyLibraryPreviewWarning.style.display = 'block';
                }
                if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.remove('hidden');
                showToast(err.message, 'error');
            } finally {
                btnDiagnoseSpotifyLibrary.disabled = false;
                btnDiagnoseSpotifyLibrary.textContent = 'Diagnose Access';
            }
        });
    }

    if (btnCancelSpotifyLibraryPreview) {
        btnCancelSpotifyLibraryPreview.addEventListener('click', () => {
            spotifyLibraryPreviewResult = null;
            if (spotifyLibraryUrlInput) spotifyLibraryUrlInput.value = '';
            if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.add('hidden');
        });
    }

    if (btnStartSpotifyLibraryImport) {
        btnStartSpotifyLibraryImport.addEventListener('click', async () => {
            const url = normalizePlaylistUrl(spotifyLibraryPreviewResult?.url || spotifyLibraryUrlInput?.value);
            if (!url) return showToast('Please preview a Spotify library playlist first', 'error');

            btnStartSpotifyLibraryImport.disabled = true;
            try {
                const response = await fetch('/api/spotify-library/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url})
                });
                const data = await readSpotifyLibraryJson(response, 'Failed to start Spotify Library Importer');

                currentSpotifyLibraryPlaylistId = data.playlist_id;
                currentSpotifyLibraryTaskId = data.task_id;
                if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.add('hidden');
                if (spotifyLibraryProgressContainer) spotifyLibraryProgressContainer.classList.remove('hidden');
                if (spotifyLibraryProgressFill) spotifyLibraryProgressFill.style.width = '0%';
                if (spotifyLibraryProgressText) spotifyLibraryProgressText.textContent = 'Starting Spotify Library Importer...';
                if (spotifyLibraryStatDownloaded) spotifyLibraryStatDownloaded.innerHTML = `<span class="stat-number">0</span><span class="stat-label">Downloaded</span>`;
                if (spotifyLibraryStatSkipped) spotifyLibraryStatSkipped.innerHTML = `<span class="stat-number">0</span><span class="stat-label">Skipped</span>`;
                if (spotifyLibraryStatFailed) spotifyLibraryStatFailed.innerHTML = `<span class="stat-number">0</span><span class="stat-label">Failed</span>`;
                if (spotifyLibraryStatusBadge) {
                    spotifyLibraryStatusBadge.textContent = 'Running';
                    spotifyLibraryStatusBadge.style.background = 'rgba(255,255,255,0.1)';
                }
                showToast('Spotify Library Importer started', 'success');
                setTimeout(pollBackgroundStatus, 500);
            } catch (err) {
                showToast(err.message, 'error');
                btnStartSpotifyLibraryImport.disabled = false;
            }
        });
    }

    if (btnCancelSpotifyLibraryImport) {
        btnCancelSpotifyLibraryImport.addEventListener('click', async () => {
            if (!confirm('Cancel this import? Any track currently downloading will stop immediately and any queued tracks will be skipped.')) return;
            try {
                btnCancelSpotifyLibraryImport.disabled = true;
                const response = await fetch('/api/spotify-library/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        playlist_id: currentSpotifyLibraryPlaylistId,
                        task_id: currentSpotifyLibraryTaskId
                    })
                });
                if (!response.ok) {
                    await readSpotifyLibraryJson(response, 'Failed to cancel Spotify Library Importer');
                }
                if (spotifyLibraryStatusBadge) {
                    spotifyLibraryStatusBadge.textContent = 'Cancelled';
                    spotifyLibraryStatusBadge.style.background = '#ff453a';
                }
                showToast('Spotify Library Importer cancellation requested', 'success');
                setTimeout(pollBackgroundStatus, 500);
            } catch (err) {
                showToast(err.message, 'error');
                btnCancelSpotifyLibraryImport.disabled = false;
            }
        });
    }

    checkSpotifyLibraryConnection();

    function escapeHtml(text) {
      if (!text) return "";
      return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    // === Download Logs Logic ===
    const downloadLogsTableBody = document.getElementById('download-logs-body');
    const logsSearchInput = document.getElementById('logs-search-input');
    let allDownloadLogs = [];

    async function loadDownloadLogs() {
        if (!downloadLogsTableBody) return;
        downloadLogsTableBody.innerHTML = `<tr><td colspan="6" class="table-placeholder">Loading download logs...</td></tr>`;
        try {
            const response = await fetch('/api/download-logs');
            if (!response.ok) throw new Error("Failed to load logs");
            allDownloadLogs = await response.json();
            renderDownloadLogs(allDownloadLogs);
        } catch (err) {
            downloadLogsTableBody.innerHTML = `<tr><td colspan="6" class="table-placeholder">Error: ${err.message}</td></tr>`;
        }
    }

    function renderDownloadLogs(logs) {
        if (!downloadLogsTableBody) return;
        downloadLogsTableBody.innerHTML = '';
        if (logs.length === 0) {
            downloadLogsTableBody.innerHTML = `<tr><td colspan="6" class="table-placeholder">No tracks found.</td></tr>`;
            return;
        }
        
        logs.forEach(track => {
            const tr = document.createElement('tr');
            const dateStr = track.timestamp ? new Date(track.timestamp).toLocaleString() : 'N/A';
            tr.innerHTML = `
                <td>${escapeHtml(track.title || 'Unknown')}</td>
                <td>${escapeHtml(track.artist || 'Unknown')}</td>
                <td><span class="badge badge-language">${escapeHtml(track.language || 'unknown')}</span></td>
                <td>${escapeHtml(track.duration || '--:--')}</td>
                <td><span style="color: var(--tertiary); font-size: 0.85rem;">${escapeHtml(track.source || 'Manual')}</span></td>
                <td style="color: var(--tertiary); font-size: 0.85rem;">${dateStr}</td>
            `;
            downloadLogsTableBody.appendChild(tr);
        });
    }

    if (logsSearchInput) {
        logsSearchInput.addEventListener('input', (e) => {
            const q = e.target.value.toLowerCase();
            const filtered = allDownloadLogs.filter(t => 
                (t.title || '').toLowerCase().includes(q) || 
                (t.artist || '').toLowerCase().includes(q) ||
                (t.source || '').toLowerCase().includes(q)
            );
            renderDownloadLogs(filtered);
        });
    }

    // === Playlist Import Logs Logic ===
    const playlistLogsOutputArea = document.getElementById('playlist-logs-output-area');
    const togglePlaylistLogsAutoRefresh = document.getElementById('toggle-playlist-logs-auto-refresh');
    const btnCopyPlaylistLogs = document.getElementById('btn-copy-playlist-logs');
    const btnRefreshPlaylistLogs = document.getElementById('btn-refresh-playlist-logs');
    let playlistLogsInterval = null;

    async function loadPlaylistLogs() {
        if (!playlistLogsOutputArea) return;
        try {
            const response = await fetch('/api/playlist/logs');
            if (!response.ok) throw new Error("Failed to fetch logs");
            const logs = await response.json();
            
            playlistLogsOutputArea.innerHTML = '';
            logs.forEach(line => {
                const lineDiv = document.createElement('div');
                lineDiv.style.marginBottom = '2px';
                
                const lowerLine = line.toLowerCase();
                if (lowerLine.includes('error') || lowerLine.includes('failed')) {
                    lineDiv.style.color = '#ff453a'; // red
                } else if (lowerLine.includes('success') || lowerLine.includes('imported') || lowerLine.includes('uploaded')) {
                    lineDiv.style.color = '#30d158'; // green
                } else if (lowerLine.includes('skipped') || lowerLine.includes('duplicate')) {
                    lineDiv.style.color = '#ffbd2e'; // yellow
                } else {
                    lineDiv.style.color = '#cccccc'; // grey/white
                }
                
                lineDiv.textContent = line;
                playlistLogsOutputArea.appendChild(lineDiv);
            });
            
            // Auto scroll to bottom
            playlistLogsOutputArea.scrollTop = playlistLogsOutputArea.scrollHeight;
        } catch (err) {
            console.error("Error loading playlist logs:", err);
        }
    }

    if (btnRefreshPlaylistLogs) {
        btnRefreshPlaylistLogs.addEventListener('click', loadPlaylistLogs);
    }

    if (togglePlaylistLogsAutoRefresh) {
        togglePlaylistLogsAutoRefresh.addEventListener('change', () => {
            if (togglePlaylistLogsAutoRefresh.checked) {
                loadPlaylistLogs();
                playlistLogsInterval = setInterval(loadPlaylistLogs, 3000);
            } else {
                if (playlistLogsInterval) {
                    clearInterval(playlistLogsInterval);
                    playlistLogsInterval = null;
                }
            }
        });
    }

    if (btnCopyPlaylistLogs) {
        btnCopyPlaylistLogs.addEventListener('click', () => {
            if (!playlistLogsOutputArea) return;
            const text = playlistLogsOutputArea.innerText;
            navigator.clipboard.writeText(text)
                .then(() => showToast("Logs copied to clipboard", "success"))
                .catch(err => showToast("Failed to copy logs: " + err.message, "error"));
        });
    }

    // === Backfill Engine Logic ===
    const backfillArtText = document.getElementById('backfill-art-text');
    const backfillDurText = document.getElementById('backfill-dur-text');
    const backfillLangText = document.getElementById('backfill-lang-text');
    const backfillStatusBadge = document.getElementById('backfill-status-badge');
    const backfillLogsOutput = document.getElementById('backfill-logs-output');
    const btnRunAllBackfill = document.getElementById('btn-run-all-backfill');
    const btnRunFullEnrichment = document.getElementById('btn-run-full-enrichment');
    const btnRunBackfillList = document.querySelectorAll('.btn-run-backfill');
    
    async function loadBackfillStatus() {
        if (!backfillArtText) return;
        try {
            const response = await fetch('/api/backfill/status');
            if (response.ok) {
                const data = await response.json();
                if (data.album_art) {
                    const running = data.running || {};
                    const art = data.album_art;
                    const duration = data.duration || {};
                    const language = data.language || {};

                    if (running.status === 'running' && running.type === 'album_art') {
                        const added = running.album_art_downloaded || 0;
                        const remaining = running.album_art_remaining ?? art.missing;
                        backfillArtText.textContent = `${remaining} missing, ${art.present} present / ${art.total} total | added ${added} this run`;
                    } else {
                        backfillArtText.textContent = `${art.missing} missing, ${art.present} present / ${art.total} total`;
                    }

                    backfillDurText.textContent = `${duration.missing || 0} missing, ${duration.present || 0} present / ${duration.total || 0} total`;
                    backfillLangText.textContent = `${language.missing || 0} missing / unknown, ${language.present || 0} present / ${language.total || 0} total`;
                }
            }
        } catch (err) {
            console.error("Failed to load backfill status:", err);
        }
    }

    async function loadBackfillLogs() {
        if (!backfillLogsOutput) return;
        try {
            const response = await fetch('/api/backfill/logs');
            if (response.ok) {
                const logs = await response.json();
                backfillLogsOutput.innerHTML = '';
                logs.forEach(line => {
                    const lineDiv = document.createElement('div');
                    lineDiv.style.marginBottom = '2px';
                    if (typeof line === 'string') {
                        lineDiv.textContent = line;
                    } else {
                        const level = String(line.level || 'info').toUpperCase();
                        const message = line.message || '';
                        lineDiv.textContent = `[${level}] ${message}`;
                        if (line.level === 'success') lineDiv.style.color = '#30d158';
                        if (line.level === 'warning') lineDiv.style.color = '#ffd60a';
                        if (line.level === 'error') lineDiv.style.color = '#ff453a';
                    }
                    backfillLogsOutput.appendChild(lineDiv);
                });
                backfillLogsOutput.scrollTop = backfillLogsOutput.scrollHeight;
            }
        } catch (err) {
            console.error("Error loading backfill logs:", err);
        }
    }

    window.cancelBackfillRun = function() {
        const cancelBtn = document.getElementById('btn-cancel-backfill');
        if (cancelBtn) {
            cancelBtn.disabled = true;
            cancelBtn.innerText = 'Cancelling...';
        }

        fetch('/api/backfill/cancel', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        })
        .then(response => response.json())
        .then(data => {
            console.log('Cancellation signal dispatched:', data);
            // Let the global status poller handle updating the badge state naturally
        })
        .catch(err => {
            console.error('Error dispatching backfill cancel:', err);
            if (cancelBtn) {
                cancelBtn.disabled = false;
                cancelBtn.innerText = 'Cancel Run';
            }
        });
    };

    async function runBackfill(type) {
        if (backfillRunning) return;
        try {
            const res = await fetch('/api/backfill/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type: type })
            });
            if (res.ok) {
                showToast(`Backfill (${type}) started!`, "success");
                backfillRunning = true;
                if (backfillStatusBadge) {
                    backfillStatusBadge.textContent = 'Running';
                    backfillStatusBadge.style.backgroundColor = 'rgba(48, 209, 88, 0.2)';
                    backfillStatusBadge.style.color = '#30d158';
                }
                
                // Trigger background poller update immediately
                setTimeout(pollBackgroundStatus, 500);
            }
        } catch (err) {
            showToast("Failed to start backfill", "error");
        }
    }

    if (btnRunAllBackfill) {
        btnRunAllBackfill.addEventListener('click', () => runBackfill('all'));
    }
    
    if (btnRunFullEnrichment) {
        btnRunFullEnrichment.addEventListener('click', async () => {
            if (backfillRunning) return;
            try {
                const res = await fetch('/api/backfill/full-enrichment', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({})
                });
                if (res.ok) {
                    showToast(`Full Enrichment started!`, "success");
                    backfillRunning = true;
                    if (backfillStatusBadge) {
                        backfillStatusBadge.textContent = 'Running';
                        backfillStatusBadge.style.backgroundColor = 'rgba(48, 209, 88, 0.2)';
                        backfillStatusBadge.style.color = '#30d158';
                    }
                    setTimeout(pollBackgroundStatus, 500);
                }
            } catch (err) {
                showToast("Failed to start full enrichment", "error");
            }
        });
    }

    btnRunBackfillList.forEach(btn => {
        btn.addEventListener('click', (e) => {
            runBackfill(e.target.dataset.type);
        });
    });

    // === Unified Global Background Task State Tracking & Polling ===
    window.backgroundStatus = null;

    function updateNavbarIndicators(status) {
        const scraperDot = document.getElementById('indicator-scraper');
        const playlistDot = document.getElementById('indicator-playlist');
        const backfillDot = document.getElementById('indicator-backfill');
        const singleDot = document.getElementById('indicator-single');

        if (scraperDot) {
            const isRunning = status.scraper.status === 'running';
            scraperDot.className = `task-indicator-dot ${isRunning ? 'active' : 'idle'}`;
            scraperDot.title = `Scraper: ${isRunning ? 'Running' : 'Idle'}`;
        }

        if (playlistDot) {
            const pl = status.playlist_import;
            const libraryImport = status.spotify_library_import || {};
            const isRunning = pl.status === 'running' || libraryImport.status === 'running';
            playlistDot.className = `task-indicator-dot ${isRunning ? 'active' : 'idle'}`;
            
            let text = 'Playlist Import: Idle';
            if (libraryImport.status === 'running') {
                const processed = libraryImport.processed || 0;
                const total = libraryImport.tracks_available_for_import || libraryImport.total_tracks || 0;
                text = `Spotify Library Importer: Running (${processed}/${total} tracks)`;
            } else if (pl.status === 'running') {
                const queueTotal = pl.queue_total || 1;
                const currentNumber = typeof pl.current_index === 'number' ? pl.current_index + 1 : 1;
                const processed = pl.processed || 0;
                const total = pl.total_tracks || 0;
                text = `Playlist Import: Running playlist ${currentNumber}/${queueTotal} (${processed}/${total} tracks)`;
            } else if (pl.status === 'completed') {
                text = 'Playlist Import: Completed';
                playlistDot.className = 'task-indicator-dot active';
            } else if (pl.status === 'completed_with_errors') {
                text = 'Playlist Import: Completed with errors';
                playlistDot.className = 'task-indicator-dot active';
            } else if (pl.status === 'cancelled') {
                text = 'Playlist Import: Cancelled';
            }
            playlistDot.title = text;
        }

        if (backfillDot) {
            const isRunning = status.backfill.status === 'running';
            backfillDot.className = `task-indicator-dot ${isRunning ? 'active' : 'idle'}`;
            backfillDot.title = `Backfill Engine: ${isRunning ? 'Running (' + status.backfill.type + ')' : 'Idle'}`;
        }

        if (singleDot) {
            const isRunning = status.single_add.status === 'running';
            singleDot.className = `task-indicator-dot ${isRunning ? 'active' : 'idle'}`;
            singleDot.title = `Single Add: ${isRunning ? 'Adding song...' : 'Idle'}`;
        }
    }

    function syncDownloaderUI(status) {
        if (!status) return;

        // 1. Scraper Card Sync
        const sc = status.scraper || {};
        if (sc.status === 'running') {
            scraperRunning = true;
            if (btnRunScraper) btnRunScraper.disabled = true;
            if (scraperSpinner) scraperSpinner.classList.remove('hidden');
            if (btnScraperText) btnScraperText.textContent = 'Running...';
        } else {
            if (scraperRunning) {
                scraperRunning = false;
                showToast('Scraper job completed successfully!', 'success');
                loadTracks(false);
                loadStorage();
            }
            if (btnRunScraper) btnRunScraper.disabled = false;
            if (scraperSpinner) scraperSpinner.classList.add('hidden');
            if (btnScraperText) btnScraperText.textContent = 'Run Scraper';
        }

        // 2. Playlist Importer Card Sync
        const pl = status.playlist_import || {};
        if (pl.status === 'running') {
            currentPlaylistId = pl.playlist_id;
            currentPlaylistQueueId = pl.queue_id;
            
            // Show progress block and hide preview block
            if (playlistProgressContainer) playlistProgressContainer.classList.remove('hidden');
            if (playlistPreviewContainer) playlistPreviewContainer.classList.add('hidden');
            
            const processed = pl.processed || 0;
            const total = pl.total_tracks || 0;
            const queue = pl.queue || [];
            const queueTotal = pl.queue_total || queue.length || 1;
            const currentNumber = typeof pl.current_index === 'number' ? pl.current_index + 1 : 1;
            const finishedPlaylists = (pl.queue_completed || 0) + (pl.queue_failed || 0) + (pl.queue_cancelled || 0);
            const trackTotal = pl.queue_total_tracks || 0;
            const trackProcessed = pl.queue_processed_tracks || 0;
            const pct = trackTotal > 0 ? (trackProcessed / trackTotal) * 100 : (finishedPlaylists / queueTotal) * 100;
            
            if (playlistProgressFill) playlistProgressFill.style.width = `${pct}%`;
            if (playlistProgressText) {
                const name = pl.current_playlist_name ? `${pl.current_playlist_name} - ` : '';
                playlistProgressText.textContent = `Playlist ${currentNumber} / ${queueTotal}: ${name}${processed} / ${total} tracks`;
            }
            if (playlistStatDownloaded) playlistStatDownloaded.innerHTML = `<span class="stat-number">${pl.downloaded || 0}</span><span class="stat-label">Downloaded</span>`;
            if (playlistStatSkipped) playlistStatSkipped.innerHTML = `<span class="stat-number">${pl.skipped || 0}</span><span class="stat-label">Skipped</span>`;
            if (playlistStatFailed) playlistStatFailed.innerHTML = `<span class="stat-number">${pl.failed || 0}</span><span class="stat-label">Failed</span>`;
            renderPlaylistQueueProgress(queue, pl.current_index, pl.status);
            
            const geminiPendingBadge = document.getElementById('gemini-pending-status');
            if (geminiPendingBadge) {
                if (pl.gemini_pending > 0) {
                    geminiPendingBadge.textContent = `⏳ ${pl.gemini_pending} tracks awaiting AI analysis`;
                    geminiPendingBadge.style.display = 'inline-block';
                } else {
                    geminiPendingBadge.style.display = 'none';
                }
            }
            
            if (playlistStatusBadge) {
                playlistStatusBadge.textContent = playlistStatusLabel(pl.status);
                playlistStatusBadge.style.background = "rgba(255,255,255,0.1)";
            }
            if (btnCancelPlaylistImport) {
                btnCancelPlaylistImport.disabled = false;
                btnCancelPlaylistImport.style.display = '';
            }
        } else if (['completed', 'completed_with_errors', 'cancelled', 'failed'].includes(pl.status) && playlistProgressContainer && !playlistProgressContainer.classList.contains('hidden')) {
            // Already visible in current session and finished
            const processed = pl.processed || 0;
            const total = pl.total_tracks || 0;
            const queue = pl.queue || [];
            const queueTotal = pl.queue_total || queue.length || 1;
            const finishedPlaylists = (pl.queue_completed || 0) + (pl.queue_failed || 0) + (pl.queue_cancelled || 0);
            const trackTotal = pl.queue_total_tracks || 0;
            const trackProcessed = pl.queue_processed_tracks || 0;
            const pct = trackTotal > 0 ? (trackProcessed / trackTotal) * 100 : (finishedPlaylists / queueTotal) * 100;
            
            if (playlistProgressFill) playlistProgressFill.style.width = `${pct}%`;
            if (playlistProgressText) {
                if (pl.status === 'completed') {
                    playlistProgressText.textContent = `Queue finished: ${pl.queue_completed || queueTotal} / ${queueTotal} playlists`;
                } else if (pl.status === 'completed_with_errors') {
                    playlistProgressText.textContent = `Queue finished with ${pl.queue_failed || 0} failed playlist${(pl.queue_failed || 0) === 1 ? '' : 's'}`;
                } else if (pl.status === 'cancelled') {
                    playlistProgressText.textContent = `Queue cancelled after ${finishedPlaylists} / ${queueTotal} playlists`;
                } else if (pl.status === 'failed') {
                    playlistProgressText.textContent = `Playlist queue failed`;
                }
            }
            if (playlistStatDownloaded) playlistStatDownloaded.innerHTML = `<span class="stat-number">${pl.downloaded || 0}</span><span class="stat-label">Downloaded</span>`;
            if (playlistStatSkipped) playlistStatSkipped.innerHTML = `<span class="stat-number">${pl.skipped || 0}</span><span class="stat-label">Skipped</span>`;
            if (playlistStatFailed) playlistStatFailed.innerHTML = `<span class="stat-number">${pl.failed || 0}</span><span class="stat-label">Failed</span>`;
            renderPlaylistQueueProgress(queue, pl.current_index, pl.status);

            if (playlistStatusBadge) {
                playlistStatusBadge.textContent = playlistStatusLabel(pl.status);
                if (pl.status === 'completed') playlistStatusBadge.style.background = "#30d158";
                else if (pl.status === 'completed_with_errors') playlistStatusBadge.style.background = "#ffbd2e";
                else playlistStatusBadge.style.background = "#ff453a";
            }
            if (btnCancelPlaylistImport) {
                btnCancelPlaylistImport.disabled = true;
                btnCancelPlaylistImport.style.display = 'none';
            }
        } else {
            // Idle or not running on load
            if (playlistProgressContainer && !playlistProgressContainer.classList.contains('hidden')) {
                playlistProgressContainer.classList.add('hidden');
            }
            renderPlaylistQueueProgress([]);
        }

        // 3. Spotify Library Importer Card Sync
        const sli = status.spotify_library_import || {};
        if (sli.status === 'running') {
            currentSpotifyLibraryPlaylistId = sli.playlist_id;
            currentSpotifyLibraryTaskId = sli.task_id;
            if (spotifyLibraryProgressContainer) spotifyLibraryProgressContainer.classList.remove('hidden');
            if (spotifyLibraryPreviewContainer) spotifyLibraryPreviewContainer.classList.add('hidden');

            const processed = sli.processed || 0;
            const total = sli.tracks_available_for_import || sli.total_tracks || 0;
            const pct = total > 0 ? Math.min(100, (processed / total) * 100) : 0;
            if (spotifyLibraryProgressFill) spotifyLibraryProgressFill.style.width = `${pct}%`;
            if (spotifyLibraryProgressText) {
                const name = sli.current_playlist_name ? `${sli.current_playlist_name} - ` : '';
                spotifyLibraryProgressText.textContent = `${name}${processed} / ${total} tracks`;
            }
            if (spotifyLibraryStatDownloaded) spotifyLibraryStatDownloaded.innerHTML = `<span class="stat-number">${sli.downloaded || 0}</span><span class="stat-label">Downloaded</span>`;
            if (spotifyLibraryStatSkipped) spotifyLibraryStatSkipped.innerHTML = `<span class="stat-number">${sli.skipped || 0}</span><span class="stat-label">Skipped</span>`;
            if (spotifyLibraryStatFailed) spotifyLibraryStatFailed.innerHTML = `<span class="stat-number">${sli.failed || 0}</span><span class="stat-label">Failed</span>`;
            if (spotifyLibraryGeminiStatus) {
                if ((sli.gemini_pending || 0) > 0) {
                    spotifyLibraryGeminiStatus.textContent = `${sli.gemini_pending} tracks awaiting AI analysis`;
                    spotifyLibraryGeminiStatus.style.display = 'inline-block';
                } else if ((sli.gemini_deferred || 0) > 0) {
                    spotifyLibraryGeminiStatus.textContent = `${sli.gemini_deferred} tracks deferred for AI retry`;
                    spotifyLibraryGeminiStatus.style.display = 'inline-block';
                } else {
                    spotifyLibraryGeminiStatus.style.display = 'none';
                }
            }
            if (spotifyLibraryStatusBadge) {
                spotifyLibraryStatusBadge.textContent = playlistStatusLabel(sli.status);
                spotifyLibraryStatusBadge.style.background = 'rgba(255,255,255,0.1)';
            }
            if (btnCancelSpotifyLibraryImport) btnCancelSpotifyLibraryImport.disabled = false;
        } else if (['completed', 'cancelled', 'failed'].includes(sli.status) && spotifyLibraryProgressContainer && !spotifyLibraryProgressContainer.classList.contains('hidden')) {
            // Already visible in current session and finished
            const processed = sli.processed || 0;
            const total = sli.tracks_available_for_import || sli.total_tracks || 0;
            const pct = total > 0 ? Math.min(100, (processed / total) * 100) : 0;
            if (spotifyLibraryProgressFill) spotifyLibraryProgressFill.style.width = `${pct}%`;
            if (spotifyLibraryProgressText) {
                const name = sli.current_playlist_name ? `${sli.current_playlist_name} - ` : '';
                if (sli.status === 'completed') {
                    spotifyLibraryProgressText.textContent = `${name}completed: ${processed} / ${total} tracks`;
                } else if (sli.status === 'cancelled') {
                    spotifyLibraryProgressText.textContent = `${name}cancelled at ${processed} / ${total} tracks`;
                } else if (sli.status === 'failed') {
                    spotifyLibraryProgressText.textContent = sli.last_error || 'Spotify Library Importer failed';
                }
            }
            if (spotifyLibraryStatDownloaded) spotifyLibraryStatDownloaded.innerHTML = `<span class="stat-number">${sli.downloaded || 0}</span><span class="stat-label">Downloaded</span>`;
            if (spotifyLibraryStatSkipped) spotifyLibraryStatSkipped.innerHTML = `<span class="stat-number">${sli.skipped || 0}</span><span class="stat-label">Skipped</span>`;
            if (spotifyLibraryStatFailed) spotifyLibraryStatFailed.innerHTML = `<span class="stat-number">${sli.failed || 0}</span><span class="stat-label">Failed</span>`;
            if (spotifyLibraryStatusBadge) {
                spotifyLibraryStatusBadge.textContent = playlistStatusLabel(sli.status);
                if (sli.status === 'completed') {
                    spotifyLibraryStatusBadge.style.background = '#30d158';
                } else {
                    spotifyLibraryStatusBadge.style.background = '#ff453a';
                }
            }
            if (btnCancelSpotifyLibraryImport) btnCancelSpotifyLibraryImport.disabled = true;
            if (btnStartSpotifyLibraryImport) btnStartSpotifyLibraryImport.disabled = false;
        } else {
            // Idle or not running on load
            if (spotifyLibraryProgressContainer && !spotifyLibraryProgressContainer.classList.contains('hidden')) {
                spotifyLibraryProgressContainer.classList.add('hidden');
            }
        }

        // 3. Backfill Engine Sync
        const bf = status.backfill;
        const btnCancelBackfill = document.getElementById('btn-cancel-backfill');
        
        if (bf.status === 'running') {
            backfillRunning = true;
            if (backfillStatusBadge) {
                backfillStatusBadge.textContent = bf.cancel_requested ? 'Cancelling' : 'Running';
                backfillStatusBadge.style.backgroundColor = 'rgba(48, 209, 88, 0.2)';
                backfillStatusBadge.style.color = '#30d158';
            }
            if (btnRunAllBackfill) btnRunAllBackfill.disabled = true;
            if (btnCancelBackfill) {
                btnCancelBackfill.disabled = !!bf.cancel_requested;
                btnCancelBackfill.innerText = bf.cancel_requested ? 'Cancelling...' : 'Cancel Run';
            }
            if (btnRunBackfillList) {
                btnRunBackfillList.forEach(btn => btn.disabled = true);
            }
            // Proactively load backfill logs and status to show progress
            loadBackfillLogs();
            loadBackfillStatus();
        } else {
            if (backfillRunning) {
                backfillRunning = false;
                showToast(bf.status === 'cancelled' ? 'Backfill task cancelled!' : 'Backfill task completed!', 'success');
                loadBackfillStatus();
                loadTracks(false);
                loadStorage();
            }
            if (backfillStatusBadge) {
                backfillStatusBadge.textContent = bf.status === 'cancelled' ? 'Cancelled' : 'Idle';
                backfillStatusBadge.style.backgroundColor = 'rgba(255, 255, 255, 0.05)';
                backfillStatusBadge.style.color = 'var(--tertiary)';
            }
            if (btnRunAllBackfill) btnRunAllBackfill.disabled = false;
            if (btnCancelBackfill) {
                btnCancelBackfill.disabled = true;
                btnCancelBackfill.innerText = 'Cancel Run';
            }
            if (btnRunBackfillList) {
                btnRunBackfillList.forEach(btn => btn.disabled = false);
            }
        }

        // 4. Single Add Sync (inside Modal)
        const sa = status.single_add;
        if (sa.status === 'running') {
            // If single add started, set state
            singleAddRunning = true;
            if (songProgressContainer) {
                songProgressContainer.classList.remove('hidden');
                songProgressText.textContent = `Downloading & importing track "${sa.track_name || 'Spotify link'}" in background...`;
            }
            if (btnPreviewSong) btnPreviewSong.disabled = true;
            if (btnConfirmAddSong) {
                btnConfirmAddSong.disabled = true;
                btnConfirmAddSong.classList.add('disabled');
            }
            if (addSongModalBtnCancel) addSongModalBtnCancel.disabled = true;
        } else {
            if (singleAddRunning) {
                singleAddRunning = false;
                showToast('Single track import completed successfully!', 'success');
                closeAddSongModal();
                loadTracks(false);
                loadStorage();
            }
        }
    }

    async function pollBackgroundStatus() {
        try {
            const response = await fetch('/api/background/status');
            if (!response.ok) {
                console.error(`Background status fetch failed: ${response.status} ${response.statusText}`);
                const playlistProgressText = document.getElementById('playlist-progress-text');
                if (playlistProgressText && playlistProgressText.parentElement && !playlistProgressText.parentElement.classList.contains('hidden')) {
                    playlistProgressText.textContent = "Unable to fetch progress, retrying...";
                }
                return;
            }
            const status = await response.json();
            window.backgroundStatus = status;
            
            updateNavbarIndicators(status);
            syncDownloaderUI(status);
        } catch (err) {
            console.error("Failed to query background tasks status:", err);
        }
    }

    // === App Imports Logic ===
    const appImportsTableBody = document.getElementById('app-imports-table-body');
    const appImportsSearchInput = document.getElementById('app-imports-search-input');
    const appImportsDeviceFilter = document.getElementById('app-imports-device-filter');
    const appImportsTotal = document.getElementById('app-imports-total');
    let allAppImports = [];

    async function loadAppImports() {
        if (!appImportsTableBody) return;
        appImportsTableBody.innerHTML = `<tr><td colspan="8" class="table-placeholder">Loading app imports...</td></tr>`;
        try {
            const response = await fetch('/api/app-imports');
            if (!response.ok) throw new Error("Failed to load app imports");
            allAppImports = await response.json();
            
            // Populate unique device IDs
            const deviceIds = new Set();
            allAppImports.forEach(t => {
                if (t.requestedBy) deviceIds.add(t.requestedBy);
            });
            
            const currentFilter = appImportsDeviceFilter.value;
            appImportsDeviceFilter.innerHTML = '<option value="">All Devices</option>';
            Array.from(deviceIds).sort().forEach(dev => {
                const opt = document.createElement('option');
                opt.value = dev;
                opt.textContent = dev.substring(0, 12) + (dev.length > 12 ? '...' : '');
                appImportsDeviceFilter.appendChild(opt);
            });
            appImportsDeviceFilter.value = currentFilter;

            renderAppImports();
        } catch (err) {
            appImportsTableBody.innerHTML = `<tr><td colspan="8" class="table-placeholder text-danger">Error: ${escapeHTML(err.message)}</td></tr>`;
        }
    }

    function renderAppImports() {
        if (!appImportsTableBody) return;
        
        const q = (appImportsSearchInput ? appImportsSearchInput.value : '').toLowerCase().trim();
        const deviceFilter = appImportsDeviceFilter ? appImportsDeviceFilter.value : '';
        
        let filtered = allAppImports.filter(t => {
            const matchesSearch = (t.title || '').toLowerCase().includes(q) || 
                                  (t.artist || '').toLowerCase().includes(q) ||
                                  (t.requestedBy || '').toLowerCase().includes(q);
            const matchesDevice = !deviceFilter || t.requestedBy === deviceFilter;
            return matchesSearch && matchesDevice;
        });
        
        if (appImportsTotal) appImportsTotal.textContent = filtered.length;
        
        appImportsTableBody.innerHTML = '';
        if (filtered.length === 0) {
            appImportsTableBody.innerHTML = `<tr><td colspan="8" class="table-placeholder">No app imports found.</td></tr>`;
            return;
        }
        
        filtered.forEach(track => {
            const tr = document.createElement('tr');
            
            const title = track.title || 'Unknown Title';
            const artist = track.artist || 'Unknown Artist';
            const duration = track.duration || '--:--';
            const lang = track.language || 'unknown';
            const source = track.source || 'unknown';
            let reqBy = track.requestedBy || 'unknown';
            if (reqBy.length > 12) reqBy = reqBy.substring(0, 12) + '...';
            
            let dateAdded = 'Unknown';
            const timeVal = track.addedAt || track.timestamp;
            if (timeVal) {
                const dateObj = new Date(timeVal);
                if (!isNaN(dateObj.getTime())) {
                    dateAdded = dateObj.toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
                }
            }

            const albumArtUrl = track.album_art || track.albumArt;
            let artHtml = '';
            if (albumArtUrl) {
                artHtml = `<img class="track-artwork" src="${escapeHTML(albumArtUrl)}" alt="${escapeHTML(title)}" loading="lazy">`;
            } else {
                artHtml = `<div class="track-art-placeholder">🎵</div>`;
            }

            tr.innerHTML = `
                <td class="artwork-col">${artHtml}</td>
                <td class="title-cell-wrap">
                    <span class="track-title-bold">${escapeHTML(title)}</span>
                </td>
                <td>${escapeHTML(artist)}</td>
                <td><span class="badge badge-language">${escapeHTML(lang)}</span></td>
                <td>${escapeHTML(duration)}</td>
                <td><span style="color: var(--tertiary); font-size: 0.85rem;">${escapeHTML(source)}</span></td>
                <td title="${escapeHTML(track.requestedBy || '')}">${escapeHTML(reqBy)}</td>
                <td style="color: var(--tertiary); font-size: 0.85rem;">${escapeHTML(dateAdded)}</td>
            `;
            appImportsTableBody.appendChild(tr);
        });
    }

    if (appImportsSearchInput) {
        appImportsSearchInput.addEventListener('input', renderAppImports);
    }
    
    if (appImportsDeviceFilter) {
        appImportsDeviceFilter.addEventListener('change', renderAppImports);
    }

    // Bootstrap app state
    checkConnection();
    loadTracks(true); // First load shows loader
    
    // Only initialize admin background tasks & polling if admin sections are present
    if (document.getElementById('section-downloader') || document.getElementById('section-storage')) {
        loadStorage();
        loadPlaylistLogs();
        loadBackfillStatus();
        
        // Initialize unified global polling
        pollBackgroundStatus();
        setInterval(pollBackgroundStatus, 5000);
    }

    // ==========================================
    // ARTISTS TAB LOGIC
    // ==========================================
    
    let allArtists = [];
    
    function renderArtistsGrid() {
        const grid = document.getElementById('artists-grid');
        if (!grid) return;
        
        const sortSelect = document.getElementById('artists-sort-select');
        const sortMode = sortSelect ? sortSelect.value : 'count_desc';
        
        // Sort allArtists
        const sortedArtists = [...allArtists];
        if (sortMode === 'count_desc') {
            sortedArtists.sort((a, b) => b.track_count - a.track_count);
        } else if (sortMode === 'alpha_asc') {
            sortedArtists.sort((a, b) => a.artist_name.localeCompare(b.artist_name));
        }
        
        if (sortedArtists.length === 0) {
            grid.innerHTML = '<div style="color: var(--tertiary);">No artists found.</div>';
            return;
        }
        
        grid.innerHTML = '';
        sortedArtists.forEach(artist => {
            const card = document.createElement('div');
            card.className = 'card artist-card';
            card.style.cursor = 'pointer';
            card.style.display = 'flex';
            card.style.flexDirection = 'column';
            card.style.alignItems = 'center';
            card.style.textAlign = 'center';
            card.style.padding = '20px';
            card.style.transition = 'transform 0.2s, background 0.2s';
            card.style.background = 'rgba(255,255,255,0.02)';
            card.style.border = '1px solid var(--border)';
            card.style.borderRadius = '12px';
            
            card.onmouseover = () => { card.style.background = 'rgba(255,255,255,0.05)'; card.style.transform = 'translateY(-2px)'; };
            card.onmouseout = () => { card.style.background = 'rgba(255,255,255,0.02)'; card.style.transform = 'translateY(0)'; };
            
            // Set hash to open artist
            card.onclick = () => {
                window.location.hash = '#artist=' + encodeURIComponent(artist.artist_name);
            };
            
            let imgHtml = '<div style="width: 80px; height: 80px; border-radius: 50%; background: #222; margin-bottom: 12px; display: flex; align-items: center; justify-content: center; color: var(--tertiary); font-size: 24px;">?</div>';
            if (artist.cover_image) {
                imgHtml = `<img src="${artist.cover_image}" alt="${artist.artist_name}" style="width: 80px; height: 80px; border-radius: 50%; object-fit: cover; margin-bottom: 12px; border: 2px solid rgba(255,255,255,0.1);">`;
            }
            
            card.innerHTML = `
                ${imgHtml}
                <h4 style="margin: 0 0 4px 0; color: #fff; font-size: 1rem;">${escapeHTML(artist.artist_name)}</h4>
                <span class="badge" style="background: rgba(255,255,255,0.1); color: var(--tertiary); font-size: 0.8rem; padding: 2px 8px; border-radius: 12px;">${artist.track_count} Track${artist.track_count !== 1 ? 's' : ''}</span>
            `;
            
            grid.appendChild(card);
        });
    }

    async function loadArtists(query = '') {
        const grid = document.getElementById('artists-grid');
        const badge = document.getElementById('artists-total-badge');
        if (!grid) return;
        
        grid.innerHTML = '<div style="color: var(--tertiary);">Loading artists...</div>';
        
        try {
            let url = '/api/artists';
            if (query) {
                url = '/api/artists/search?q=' + encodeURIComponent(query);
            }
            
            const response = await fetch(url);
            if (!response.ok) throw new Error('Failed to load artists');
            
            allArtists = await response.json();
            
            if (badge && !query) {
                badge.textContent = `${allArtists.length} Total`;
            }
            
            renderArtistsGrid();
            
        } catch (err) {
            grid.innerHTML = `<div style="color: #ff453a;">Error: ${err.message}</div>`;
            showToast('Error loading artists', 'error');
        }
    }
    
    // Sort Dropdown
    const sortSelect = document.getElementById('artists-sort-select');
    if (sortSelect) {
        sortSelect.addEventListener('change', renderArtistsGrid);
    }
    
    // Artist Search Input Event
    const artistsSearchInput = document.getElementById('artists-search-input');
    if (artistsSearchInput) {
        let artistsSearchTimeout = null;
        artistsSearchInput.addEventListener('input', (e) => {
            clearTimeout(artistsSearchTimeout);
            artistsSearchTimeout = setTimeout(() => {
                loadArtists(e.target.value);
            }, 300);
        });
    }
    
    // Artist Detail View Logic
    const artistDetailView = document.getElementById('artist-detail-view');
    const artistsGrid = document.getElementById('artists-grid');
    const btnBackToArtists = document.getElementById('btn-back-to-artists');
    
    if (btnBackToArtists) {
        btnBackToArtists.addEventListener('click', () => {
            window.location.hash = '#artists';
        });
    }
    
    async function openArtistTracks(artistName) {
        if (artistsGrid) artistsGrid.style.display = 'none';
        if (artistDetailView) artistDetailView.style.display = 'block';
        
        const nameEl = document.getElementById('artist-detail-name');
        const countEl = document.getElementById('artist-detail-count');
        const imgEl = document.getElementById('artist-detail-image');
        const listEl = document.getElementById('artist-detail-tracks-list');
        
        nameEl.textContent = artistName;
        countEl.textContent = 'Loading...';
        listEl.innerHTML = '<div style="color: var(--tertiary); padding: 16px 0;">Loading tracks...</div>';
        imgEl.src = '';
        
        try {
            // Find artist in allArtists to set cover image quickly
            const artistData = allArtists.find(a => a.artist_name === artistName);
            if (artistData && artistData.cover_image) {
                imgEl.src = artistData.cover_image;
            } else {
                imgEl.src = "data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120' viewBox='0 0 24 24' fill='none' stroke='%23666' stroke-width='1'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M12 8v4l3 3'/%3E%3C/svg%3E";
            }

            const response = await fetch('/api/artists/' + encodeURIComponent(artistName));
            if (!response.ok) throw new Error('Failed to load artist tracks');
            
            const tracks = await response.json();
            countEl.textContent = `${tracks.length} Track${tracks.length !== 1 ? 's' : ''}`;
            
            if (tracks.length === 0) {
                listEl.innerHTML = '<div style="color: var(--tertiary); padding: 16px 0;">No tracks found.</div>';
                return;
            }
            
            listEl.innerHTML = '';
            tracks.forEach(track => {
                const row = document.createElement('div');
                row.style.display = 'flex';
                row.style.alignItems = 'center';
                row.style.gap = '16px';
                row.style.padding = '12px 16px';
                row.style.background = 'rgba(255,255,255,0.02)';
                row.style.border = '1px solid rgba(255,255,255,0.05)';
                row.style.borderRadius = '8px';
                
                let artHtml = '<div style="width: 48px; height: 48px; background: #222; border-radius: 6px;"></div>';
                if (track.album_art) {
                    artHtml = `<img src="${track.album_art}" style="width: 48px; height: 48px; object-fit: cover; border-radius: 6px;">`;
                }
                
                const langBadgeColor = track.language === 'unknown' ? '#666' : '#0a84ff';
                const langBadge = `<span class="badge" style="background: ${langBadgeColor}20; color: ${langBadgeColor}; border: 1px solid ${langBadgeColor}40; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem;">${escapeHTML(track.language || 'unknown')}</span>`;
                
                const genreBadgeColor = track.genre === 'Unknown' ? '#666' : '#30d158';
                const genreBadge = `<span class="badge" style="background: ${genreBadgeColor}20; color: ${genreBadgeColor}; border: 1px solid ${genreBadgeColor}40; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem;">${escapeHTML(track.genre || 'Unknown')}</span>`;

                row.innerHTML = `
                    ${artHtml}
                    <div style="flex: 1; display: flex; flex-direction: column; gap: 4px;">
                        <div style="color: #fff; font-weight: 500;">${escapeHTML(track.title)}</div>
                        <div style="color: var(--tertiary); font-size: 0.85rem;">${escapeHTML(track.album || 'Unknown Album')}</div>
                    </div>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end;">
                        ${langBadge}
                        ${genreBadge}
                    </div>
                    <div style="color: var(--tertiary); font-size: 0.85rem; width: 60px; text-align: right;">
                        ${track.duration || '--:--'}
                    </div>
                `;
                listEl.appendChild(row);
            });
            
        } catch (err) {
            listEl.innerHTML = `<div style="color: #ff453a; padding: 16px 0;">Error: ${err.message}</div>`;
            showToast('Error loading artist tracks', 'error');
        }
    }

    // Routing Logic for Deep Linking
    async function handleHashChange() {
        const hash = window.location.hash;
        if (hash.startsWith('#artist=')) {
            const artistName = decodeURIComponent(hash.substring(8));
            
            // 1. Ensure the Artists section is visible FIRST
            const btnArtists = document.getElementById('nav-btn-artists');
            const sectionArtists = document.getElementById('section-artists');
            
            // If the section isn't active, activate it.
            if (sectionArtists && !sectionArtists.classList.contains('active')) {
                if (btnArtists) {
                    btnArtists.click(); // This will trigger loadArtists() in the background
                } else {
                    // Fallback just in case btn isn't found
                    document.querySelectorAll('.content-section').forEach(s => s.classList.remove('active'));
                    sectionArtists.classList.add('active');
                }
            }
            
            // 2. Await data load if necessary to avoid race conditions
            if (allArtists.length === 0) {
                await loadArtists();
            }
            
            // 3. Now safely open the detail view
            openArtistTracks(artistName);
            
        } else {
            if (artistDetailView) artistDetailView.style.display = 'none';
            if (artistsGrid) artistsGrid.style.display = 'grid';
        }
    }
    
    window.addEventListener('hashchange', handleHashChange);
    // Initial check on load
    setTimeout(handleHashChange, 100);
    
    window.loadArtists = loadArtists;

    // --- Data Health / Field Completeness ---
    const btnRefreshDataHealth = document.getElementById('btn-refresh-data-health');
    if (btnRefreshDataHealth) {
        btnRefreshDataHealth.addEventListener('click', loadDataHealth);
    }

    const missingTracksViewer = document.getElementById('missing-tracks-viewer');
    const btnCloseMissingTracks = document.getElementById('btn-close-missing-tracks');
    if (btnCloseMissingTracks) {
        btnCloseMissingTracks.addEventListener('click', () => {
            missingTracksViewer.classList.add('hidden');
        });
    }

    async function loadDataHealth() {
        const tbody = document.getElementById('data-health-body');
        if (!tbody) return;
        
        tbody.innerHTML = '<tr><td colspan="4" class="table-placeholder">Loading field completeness...</td></tr>';
        
        try {
            const response = await fetch('/api/library/field-completeness');
            const data = await response.json();
            
            if (data.error) {
                tbody.innerHTML = `<tr><td colspan="4" class="table-placeholder error">Error: ${data.error}</td></tr>`;
                return;
            }
            
            const timeSpan = document.getElementById('data-health-update-time');
            if (timeSpan && data.generated_at) {
                const dt = new Date(data.generated_at);
                timeSpan.textContent = `(Last updated: ${dt.toLocaleTimeString()})`;
            }
            
            let fieldsArray = Object.keys(data.fields).map(key => ({
                name: key,
                ...data.fields[key]
            }));
            
            // Sort by percentage ascending (worst first)
            fieldsArray.sort((a, b) => a.percentage - b.percentage);
            
            tbody.innerHTML = '';
            
            fieldsArray.forEach(field => {
                const tr = document.createElement('tr');
                
                // Field Name
                const tdName = document.createElement('td');
                tdName.textContent = field.name;
                tdName.style.fontWeight = '500';
                
                // Completeness
                const tdComp = document.createElement('td');
                tdComp.innerHTML = `
                    <div style="display: flex; align-items: center; gap: 12px; width: 100%;">
                        <div class="progress-bar-container" style="flex: 1; max-width: 200px; height: 8px;">
                            <div class="progress-bar-fill" style="width: ${field.percentage}%; background-color: ${field.percentage === 100 ? '#30d158' : field.percentage > 50 ? '#ffbd2e' : '#ff453a'}"></div>
                        </div>
                        <span style="font-size: 0.85rem; color: var(--tertiary);">${field.percentage.toFixed(1)}%</span>
                    </div>
                `;
                
                // Present / Missing
                const tdCounts = document.createElement('td');
                const presentSpan = document.createElement('span');
                presentSpan.style.color = 'var(--text-primary)';
                presentSpan.textContent = field.present;
                
                const missingSpan = document.createElement('a');
                missingSpan.href = 'javascript:void(0)';
                missingSpan.style.color = field.missing > 0 ? '#ff453a' : 'var(--tertiary)';
                missingSpan.style.textDecoration = field.missing > 0 ? 'underline' : 'none';
                missingSpan.style.cursor = field.missing > 0 ? 'pointer' : 'default';
                missingSpan.textContent = field.missing;
                
                if (field.missing > 0) {
                    missingSpan.addEventListener('click', () => {
                        loadMissingTracks(field.name);
                    });
                }
                
                tdCounts.appendChild(presentSpan);
                tdCounts.appendChild(document.createTextNode(' / '));
                tdCounts.appendChild(missingSpan);
                
                // Actions
                const tdActions = document.createElement('td');
                const fieldBackfillActions = {
                    'album_art': { type: 'album_art', label: 'Backfill Now' },
                    'albumArt': { type: 'album_art', label: 'Backfill Now' },
                    'durationSeconds': { type: 'duration', label: 'Backfill Now' },
                    'duration': { type: 'duration', label: 'Backfill Now' },
                    'language': { type: 'gemini', label: 'Run Gemini' },
                    'genre': { type: 'gemini', label: 'Run Gemini' },
                    'album': { type: 'all', label: 'Run Complete' },
                    'lyrics': { type: 'all', label: 'Run Complete' },
                    'syncedLyrics': { type: 'all', label: 'Run Complete' },
                    'lyricsStatus': { type: 'lyrics_status', label: 'Backfill Now' },
                    'source': { type: 'normalize', label: 'Normalize' },
                    'addedAt': { type: 'normalize', label: 'Normalize' },
                    'updatedAt': { type: 'normalize', label: 'Normalize' }
                };

                const fieldNotes = {
                    'requestedBy': 'Optional app-import field',
                    'spotify_id': 'Not safely recoverable',
                    'id': 'Required identifier',
                    'driveFileId': 'Required identifier',
                    'url': 'Derived at runtime',
                    'size': 'Drive metadata'
                };
                const action = fieldBackfillActions[field.name];
                
                if (field.percentage < 100 && action) {
                    const btn = document.createElement('button');
                    btn.className = 'btn btn-secondary';
                    btn.style.padding = '4px 10px';
                    btn.style.fontSize = '0.75rem';
                    btn.textContent = action.label;
                    btn.onclick = () => {
                        runDataHealthBackfill(action.type, field.name);
                    };
                    tdActions.appendChild(btn);
                } else if (field.percentage < 100) {
                    const span = document.createElement('span');
                    span.className = 'badge';
                    span.style.backgroundColor = 'transparent';
                    span.style.color = 'var(--tertiary)';
                    span.style.border = '1px dashed var(--border)';
                    span.textContent = fieldNotes[field.name] || 'No automatic backfill';
                    span.title = fieldNotes[field.name] || 'This field has no reliable automatic backfill path yet.';
                    tdActions.appendChild(span);
                } else {
                    const span = document.createElement('span');
                    span.className = 'badge badge-genre';
                    span.textContent = 'Complete';
                    tdActions.appendChild(span);
                }
                
                tr.appendChild(tdName);
                tr.appendChild(tdComp);
                tr.appendChild(tdCounts);
                tr.appendChild(tdActions);
                
                tbody.appendChild(tr);
            });
            
        } catch (error) {
            console.error("Error loading data health:", error);
            tbody.innerHTML = `<tr><td colspan="4" class="table-placeholder error">Failed to load data.</td></tr>`;
        }
    }

    async function loadMissingTracks(fieldName) {
        if (missingTracksViewer) missingTracksViewer.classList.remove('hidden');
        const list = document.getElementById('missing-tracks-list');
        const title = document.getElementById('missing-tracks-title');
        
        list.innerHTML = '<li>Loading...</li>';
        title.textContent = `Missing Tracks: ${fieldName}`;
        
        try {
            const response = await fetch(`/api/library/field-completeness/${fieldName}/missing-tracks`);
            const data = await response.json();
            
            list.innerHTML = '';
            if (data.length === 0) {
                list.innerHTML = '<li>No tracks missing this field.</li>';
                return;
            }
            
            data.forEach(track => {
                const li = document.createElement('li');
                li.style.padding = '8px 0';
                li.style.borderBottom = '1px solid var(--border)';
                li.textContent = `${track.title} — ${track.artist}`;
                list.appendChild(li);
            });
        } catch (error) {
            list.innerHTML = `<li>Error loading tracks: ${error.message}</li>`;
        }
    }

    async function runDataHealthBackfill(btype, fieldName) {
        showToast(`Starting backfill for ${fieldName}...`);
        
        // Ensure any running backfill is cancelled
        if (window.cancelBackfillRun) {
             try { window.cancelBackfillRun(); } catch(e){} // fire and forget
        }
        
        try {
            const endpoint = btype === 'gemini' ? '/api/backfill/gemini' : '/api/backfill/run';
            const payload = btype === 'gemini' ? { mode: 'auto' } : { type: btype };
            const response = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            
            const data = await response.json();
            if (response.ok) {
                showToast(`Backfill started!`);
                // Wait for it to finish then refresh
                pollUntilBackfillDone();
            } else {
                showToast(`Failed: ${data.error || data.status}`, true);
            }
        } catch (error) {
            showToast(`Error starting backfill`, true);
        }
    }

    function pollUntilBackfillDone() {
        const interval = setInterval(async () => {
            try {
                const response = await fetch('/api/background/status');
                const data = await response.json();
                if (data.backfill && data.backfill.status === 'idle') {
                    clearInterval(interval);
                    showToast('Backfill completed!');
                    loadDataHealth();
                }
            } catch (error) {
                console.error("Polling error:", error);
            }
        }, 2000);
    }

});
