document.addEventListener('DOMContentLoaded', () => {
    // Global State
    let allTracks = [];
    let searchQuery = '';
    let sortColumn = 'index';
    let sortDirection = 'asc';
    let isScraperPolling = false;
    let scraperPollIntervalId = null;
    let trackToDeleteFileId = null;

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
    
    async function checkScraperStatus() {
        try {
            const response = await fetch('/api/scraper/status');
            if (!response.ok) return;
            const data = await response.json();
            
            if (data.status === 'running') {
                if (!isScraperPolling) {
                    // Start polling UI
                    isScraperPolling = true;
                    btnRunScraper.disabled = true;
                    scraperSpinner.classList.remove('hidden');
                    btnScraperText.textContent = 'Running...';
                    
                    // Poll status every 4 seconds
                    scraperPollIntervalId = setInterval(async () => {
                        const res = await fetch('/api/scraper/status');
                        const statusData = await res.json();
                        if (statusData.status === 'idle') {
                            // Scraper finished!
                            clearInterval(scraperPollIntervalId);
                            isScraperPolling = false;
                            
                            // Restore UI
                            btnRunScraper.disabled = false;
                            scraperSpinner.classList.add('hidden');
                            btnScraperText.textContent = 'Run Scraper';
                            
                            showToast('Scraper job completed successfully!', 'success');
                            // Refresh page data immediately
                            loadTracks(false);
                            loadStorage();
                        }
                    }, 4000);
                }
            } else {
                // If idle, make sure UI matches
                if (isScraperPolling) {
                    clearInterval(scraperPollIntervalId);
                    isScraperPolling = false;
                }
                btnRunScraper.disabled = false;
                scraperSpinner.classList.add('hidden');
                btnScraperText.textContent = 'Run Scraper';
            }
        } catch (err) {
            console.error('Failed to query scraper status:', err);
        }
    }

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
                    setTimeout(checkScraperStatus, 1000);
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
            songProgressText.textContent = "Downloading track from YouTube & uploading to Google Drive. Please wait, this may take a moment...";
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

            showToast(`"${data.track.title}" added to your library successfully!`, 'success');
            closeAddSongModal();
            
            // Refresh library tracks list and metrics in background
            loadTracks(false);
            loadStorage();
        } catch (err) {
            showAddSongStatus(`Error: ${err.message}`, "error");
        } finally {
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
                
                startPlaylistPolling();
                showToast("Playlist import started in the background", "success");
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
                showToast("Cancellation requested", "success");
            } catch (err) {
                showToast(err.message, "error");
            }
        });
    }

    function startPlaylistPolling() {
        if (playlistPollInterval) clearInterval(playlistPollInterval);
        playlistPollInterval = setInterval(pollPlaylistStatus, 3000);
    }

    async function pollPlaylistStatus() {
        if (!currentPlaylistId) return;
        try {
            const response = await fetch(`/api/playlist/status?playlist_id=${currentPlaylistId}`);
            const data = await response.json();
            
            if (data.status === "not_found") return; // Might be initializing
            
            const processed = data.processed || 0;
            const total = data.total_tracks || 0;
            const pct = total > 0 ? (processed / total) * 100 : 0;
            
            playlistProgressFill.style.width = `${pct}%`;
            playlistProgressText.textContent = `Processed: ${processed} / ${total}`;
            playlistStatDownloaded.textContent = `Downloaded: ${data.downloaded || 0}`;
            playlistStatSkipped.textContent = `Skipped: ${data.skipped || 0}`;
            playlistStatFailed.textContent = `Failed: ${data.failed || 0}`;
            
            if (data.status === "completed") {
                clearInterval(playlistPollInterval);
                playlistStatusBadge.textContent = "Completed";
                playlistStatusBadge.style.background = "#30d158";
                showToast("Playlist import completed!", "success");
                loadTracks(false);
                loadStorage();
            } else if (data.status === "cancelled") {
                clearInterval(playlistPollInterval);
                playlistStatusBadge.textContent = "Cancelled";
                playlistStatusBadge.style.background = "#ff453a";
            }
        } catch (err) {
            console.error("Error polling playlist status:", err);
        }
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

    // Bootstrap app state
    checkConnection();
    loadTracks(true); // First load shows loader
    loadStorage();
    checkScraperStatus();
});
