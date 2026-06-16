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

        if (filteredTracks.length === 0) {
            tracksTableBody.innerHTML = `<tr><td colspan="8" class="table-placeholder">${query ? 'No matching tracks found.' : 'No tracks found. Run the scraper to populate database.'}</td></tr>`;
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
                <td class="actions-col">
                    <button class="btn btn-danger btn-delete-track" data-id="${fileId}" data-title="${escapeHTML(title)}">Delete</button>
                </td>
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
    const inputPlaylistUrl = document.getElementById('input-playlist-url');
    const btnPreviewPlaylist = document.getElementById('btn-preview-playlist');
    const playlistPreviewContainer = document.getElementById('playlist-preview-container');
    const previewPlaylistName = document.getElementById('preview-playlist-name');
    const previewPlaylistCount = document.getElementById('preview-playlist-count');
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
    
    let currentPlaylistId = null;
    let playlistPollInterval = null;

    if (btnPreviewPlaylist) {
        btnPreviewPlaylist.addEventListener('click', async () => {
            const url = inputPlaylistUrl.value.trim();
            if (!url) return showToast("Please enter a playlist URL", "error");
            
            btnPreviewPlaylist.disabled = true;
            btnPreviewPlaylist.textContent = "Loading...";
            
            try {
                const response = await fetch('/api/playlist/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url})
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || "Failed to preview playlist");
                
                previewPlaylistName.textContent = data.playlist_name;
                previewPlaylistCount.textContent = `${data.total_tracks} Tracks`;
                previewPlaylistSize.textContent = data.estimated_size_display;
                
                previewTracksList.innerHTML = data.preview_tracks.map(t => `<div>• ${escapeHtml(t.title)} - ${escapeHtml(t.artist)}</div>`).join('') + (data.total_tracks > 5 ? `<div>...and ${data.total_tracks - 5} more</div>` : '');
                
                playlistPreviewContainer.classList.remove('hidden');
                playlistProgressContainer.classList.add('hidden');
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                btnPreviewPlaylist.disabled = false;
                btnPreviewPlaylist.textContent = "Preview Playlist";
            }
        });
    }

    if (btnCancelPlaylistPreview) {
        btnCancelPlaylistPreview.addEventListener('click', () => {
            playlistPreviewContainer.classList.add('hidden');
            inputPlaylistUrl.value = '';
        });
    }

    if (btnStartPlaylistImport) {
        btnStartPlaylistImport.addEventListener('click', async () => {
            const url = inputPlaylistUrl.value.trim();
            btnStartPlaylistImport.disabled = true;
            
            try {
                const response = await fetch('/api/playlist/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url})
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || "Failed to start import");
                
                currentPlaylistId = data.playlist_id;
                playlistPreviewContainer.classList.add('hidden');
                playlistProgressContainer.classList.remove('hidden');
                
                // reset progress
                playlistProgressFill.style.width = '0%';
                playlistProgressText.textContent = `Processed: 0 / 0`;
                playlistStatDownloaded.textContent = `Downloaded: 0`;
                playlistStatSkipped.textContent = `Skipped: 0`;
                playlistStatFailed.textContent = `Failed: 0`;
                playlistStatusBadge.textContent = "Running";
                playlistStatusBadge.style.background = "rgba(255,255,255,0.1)";
                
                showToast("Playlist import started in the background", "success");
                setTimeout(pollBackgroundStatus, 500);
            } catch (err) {
                showToast(err.message, "error");
            } finally {
                btnStartPlaylistImport.disabled = false;
            }
        });
    }

    if (btnCancelPlaylistImport) {
        btnCancelPlaylistImport.addEventListener('click', async () => {
            if (!currentPlaylistId) return;
            try {
                await fetch('/api/playlist/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({playlist_id: currentPlaylistId})
                });
                playlistStatusBadge.textContent = "Cancelled";
                playlistStatusBadge.style.background = "#ff453a";
                showToast("Import cancelled", "success");
                setTimeout(pollBackgroundStatus, 500);
            } catch (err) {
                showToast(err.message, "error");
            }
        });
    }

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
                    backfillArtText.textContent = `${data.album_art.missing} missing out of ${data.album_art.total} total`;
                    backfillDurText.textContent = `${data.duration.missing} missing out of ${data.duration.total} total`;
                    backfillLangText.textContent = `${data.language.missing} missing / unknown out of ${data.language.total} total`;
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
                    lineDiv.textContent = line;
                    backfillLogsOutput.appendChild(lineDiv);
                });
                backfillLogsOutput.scrollTop = backfillLogsOutput.scrollHeight;
            }
        } catch (err) {
            console.error("Error loading backfill logs:", err);
        }
    }

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
            const isRunning = pl.status === 'running';
            playlistDot.className = `task-indicator-dot ${isRunning ? 'active' : 'idle'}`;
            
            let text = 'Playlist Import: Idle';
            if (pl.status === 'running') {
                const processed = pl.processed || 0;
                const total = pl.total_tracks || 0;
                text = `Playlist Import: Running (${processed}/${total} tracks processed)`;
            } else if (pl.status === 'completed') {
                text = 'Playlist Import: Completed';
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
        // 1. Scraper Card Sync
        const sc = status.scraper;
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
        const pl = status.playlist_import;
        if (pl.status === 'running' || pl.status === 'completed' || pl.status === 'cancelled') {
            currentPlaylistId = pl.playlist_id;
            
            // Show progress block and hide preview block
            if (playlistProgressContainer) playlistProgressContainer.classList.remove('hidden');
            if (playlistPreviewContainer) playlistPreviewContainer.classList.add('hidden');
            
            const processed = pl.processed || 0;
            const total = pl.total_tracks || 0;
            const pct = total > 0 ? (processed / total) * 100 : 0;
            
            if (playlistProgressFill) playlistProgressFill.style.width = `${pct}%`;
            if (playlistProgressText) playlistProgressText.textContent = `Processed: ${processed} / ${total}`;
            if (playlistStatDownloaded) playlistStatDownloaded.textContent = `Downloaded: ${pl.downloaded || 0}`;
            if (playlistStatSkipped) playlistStatSkipped.textContent = `Skipped: ${pl.skipped || 0}`;
            if (playlistStatFailed) playlistStatFailed.textContent = `Failed: ${pl.failed || 0}`;
            
            if (playlistStatusBadge) {
                playlistStatusBadge.textContent = pl.status.charAt(0).toUpperCase() + pl.status.slice(1);
                if (pl.status === 'running') {
                    playlistStatusBadge.style.background = "rgba(255,255,255,0.1)";
                    if (btnCancelPlaylistImport) btnCancelPlaylistImport.disabled = false;
                } else if (pl.status === 'completed') {
                    playlistStatusBadge.style.background = "#30d158";
                    if (btnCancelPlaylistImport) btnCancelPlaylistImport.disabled = true;
                } else if (pl.status === 'cancelled') {
                    playlistStatusBadge.style.background = "#ff453a";
                    if (btnCancelPlaylistImport) btnCancelPlaylistImport.disabled = true;
                }
            }
        } else {
            // Idle / Not Found
            if (playlistProgressContainer && !playlistProgressContainer.classList.contains('hidden') && pl.status === 'idle') {
                playlistProgressContainer.classList.add('hidden');
            }
        }

        // 3. Backfill Engine Sync
        const bf = status.backfill;
        if (bf.status === 'running') {
            backfillRunning = true;
            if (backfillStatusBadge) {
                backfillStatusBadge.textContent = 'Running';
                backfillStatusBadge.style.backgroundColor = 'rgba(48, 209, 88, 0.2)';
                backfillStatusBadge.style.color = '#30d158';
            }
            if (btnRunAllBackfill) btnRunAllBackfill.disabled = true;
            if (btnRunBackfillList) {
                btnRunBackfillList.forEach(btn => btn.disabled = true);
            }
            // Proactively load backfill logs and status to show progress
            loadBackfillLogs();
            loadBackfillStatus();
        } else {
            if (backfillRunning) {
                backfillRunning = false;
                showToast('Backfill task completed!', 'success');
                loadBackfillStatus();
                loadTracks(false);
                loadStorage();
            }
            if (backfillStatusBadge) {
                backfillStatusBadge.textContent = 'Idle';
                backfillStatusBadge.style.backgroundColor = 'rgba(255, 255, 255, 0.05)';
                backfillStatusBadge.style.color = 'var(--tertiary)';
            }
            if (btnRunAllBackfill) btnRunAllBackfill.disabled = false;
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
            if (!response.ok) return;
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
    loadStorage();
    loadPlaylistLogs();
    loadBackfillStatus();
    
    // Initialize unified global polling
    pollBackgroundStatus();
    setInterval(pollBackgroundStatus, 5000);

    // ==========================================
    // ARTISTS TAB LOGIC
    // ==========================================
    
    let allArtists = [];
    
    async function loadArtists(query = '') {
        const grid = document.getElementById('artists-grid');
        if (!grid) return;
        
        grid.innerHTML = '<div style="color: var(--tertiary);">Loading artists...</div>';
        
        try {
            let url = '/api/artists';
            if (query) {
                url = '/api/artists/search?q=' + encodeURIComponent(query);
            }
            
            const response = await fetch(url);
            if (!response.ok) throw new Error('Failed to load artists');
            
            const artists = await response.json();
            allArtists = artists;
            
            if (artists.length === 0) {
                grid.innerHTML = '<div style="color: var(--tertiary);">No artists found.</div>';
                return;
            }
            
            grid.innerHTML = '';
            artists.forEach(artist => {
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
                
                card.onclick = () => openArtistTracks(artist.artist_name);
                
                let imgHtml = '<div style="width: 80px; height: 80px; border-radius: 50%; background: #222; margin-bottom: 12px; display: flex; align-items: center; justify-content: center; color: var(--tertiary); font-size: 24px;">?</div>';
                if (artist.cover_image) {
                    imgHtml = `<img src="${artist.cover_image}" alt="${artist.artist_name}" style="width: 80px; height: 80px; border-radius: 50%; object-fit: cover; margin-bottom: 12px; border: 2px solid rgba(255,255,255,0.1);">`;
                }
                
                card.innerHTML = `
                    ${imgHtml}
                    <h4 style="margin: 0 0 4px 0; color: #fff; font-size: 1rem;">${artist.artist_name}</h4>
                    <span class="badge" style="background: rgba(255,255,255,0.1); color: var(--tertiary); font-size: 0.8rem; padding: 2px 8px; border-radius: 12px;">${artist.track_count} Track${artist.track_count !== 1 ? 's' : ''}</span>
                `;
                
                grid.appendChild(card);
            });
            
        } catch (err) {
            grid.innerHTML = `<div style="color: #ff453a;">Error: ${err.message}</div>`;
            showToast('Error loading artists', 'error');
        }
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
    
    // Artist Tracks Modal
    const artistTracksModal = document.getElementById('artist-tracks-modal');
    const artistTracksTitle = document.getElementById('artist-tracks-title');
    const artistTracksTableBody = document.getElementById('artist-tracks-table-body');
    const artistTracksCloseBtn = document.getElementById('artist-tracks-modal-close-x');
    const artistTracksCancelBtn = document.getElementById('modal-btn-artist-close');
    
    if (artistTracksCloseBtn) artistTracksCloseBtn.addEventListener('click', closeArtistTracksModal);
    if (artistTracksCancelBtn) artistTracksCancelBtn.addEventListener('click', closeArtistTracksModal);
    
    function closeArtistTracksModal() {
        artistTracksModal.classList.add('hidden');
    }
    
    async function openArtistTracks(artistName) {
        artistTracksModal.classList.remove('hidden');
        artistTracksTitle.textContent = artistName;
        artistTracksTableBody.innerHTML = '<tr><td colspan="4" class="table-placeholder">Loading tracks...</td></tr>';
        
        try {
            const response = await fetch('/api/artists/' + encodeURIComponent(artistName));
            if (!response.ok) throw new Error('Failed to load artist tracks');
            
            const tracks = await response.json();
            
            if (tracks.length === 0) {
                artistTracksTableBody.innerHTML = '<tr><td colspan="4" class="table-placeholder">No tracks found.</td></tr>';
                return;
            }
            
            artistTracksTableBody.innerHTML = '';
            tracks.forEach(track => {
                const tr = document.createElement('tr');
                
                let artHtml = '<div class="track-artwork-placeholder"></div>';
                if (track.album_art) {
                    artHtml = `<img src="${track.album_art}" class="track-artwork">`;
                }
                
                tr.innerHTML = `
                    <td class="artwork-col">${artHtml}</td>
                    <td>
                        <div class="track-title">${track.title}</div>
                        <div class="track-artist">${track.artist}</div>
                    </td>
                    <td><div class="track-album">${track.album || 'Unknown'}</div></td>
                    <td><div class="track-duration">${track.duration || '--:--'}</div></td>
                `;
                
                artistTracksTableBody.appendChild(tr);
            });
            
        } catch (err) {
            artistTracksTableBody.innerHTML = `<tr><td colspan="4" style="color: #ff453a; text-align: center; padding: 20px;">Error: ${err.message}</td></tr>`;
            showToast('Error loading artist tracks', 'error');
        }
    }
    
    window.loadArtists = loadArtists;

});
