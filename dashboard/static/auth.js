(function () {
    const tokenMeta = document.querySelector('meta[name="dashboard-write-token"]');
    const token = tokenMeta ? tokenMeta.getAttribute('content') : '';
    if (!token || window.__dashboardAuthFetchInstalled) {
        return;
    }

    window.__dashboardAuthFetchInstalled = true;
    const originalFetch = window.fetch.bind(window);
    const mutatingMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

    window.fetch = (input, init = {}) => {
        const requestUrl = typeof input === 'string' ? input : input.url;
        const url = new URL(requestUrl, window.location.origin);
        const method = (init.method || (typeof input !== 'string' ? input.method : 'GET') || 'GET').toUpperCase();

        if (url.origin === window.location.origin && mutatingMethods.has(method)) {
            const headers = new Headers(init.headers || (typeof input !== 'string' ? input.headers : undefined));
            if (!headers.has('X-Dashboard-Token')) {
                headers.set('X-Dashboard-Token', token);
            }
            init = { ...init, headers };
        }

        return originalFetch(input, init);
    };
})();
