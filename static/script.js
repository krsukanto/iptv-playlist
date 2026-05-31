document.addEventListener('DOMContentLoaded', () => {
    const syncDbBtn = document.getElementById('syncDbBtn');
    const m3uUrlInput = document.getElementById('m3uUrl');
    const validateCheckbox = document.getElementById('validateStreams');
    const searchNameInput = document.getElementById('searchName');
    const searchCategoryInput = document.getElementById('searchCategory');
    const statusFilterSelect = document.getElementById('statusFilter');
    const baseTabBtn = document.getElementById('baseTabBtn');
    const masterTabBtn = document.getElementById('masterTabBtn');
    const addToMasterBtn = document.getElementById('addToMasterBtn');
    const removeFromMasterBtn = document.getElementById('removeFromMasterBtn');
    const exportMasterM3uBtn = document.getElementById('exportMasterM3uBtn');
    const exportCsvBtn = document.getElementById('exportCsvBtn');
    const statusMessage = document.getElementById('statusMessage');
    const dataTableBody = document.querySelector('#dataTable tbody');

    let debounceTimer, isMasterFilterActive = false;

    // Function to update the status message
    function updateStatus(message, type = 'info') {
        statusMessage.textContent = message;
        statusMessage.className = `status ${type}`; // Add class for styling if needed
    }

    // Function to populate category dropdown
    async function loadCategories() {
        try {
            const masterOnly = isMasterFilterActive ? 'true' : 'false';
            const response = await fetch(`/api/categories?master_only=${masterOnly}`);
            const result = await response.json();
            if (result.status === 'success') {
                const currentValue = searchCategoryInput.value;
                searchCategoryInput.innerHTML = '<option value="">All Categories</option>';
                result.categories.forEach(cat => {
                    const option = document.createElement('option');
                    option.value = cat;
                    option.textContent = cat;
                    searchCategoryInput.appendChild(option);
                });
                // Restore previous selection if it still exists
                searchCategoryInput.value = currentValue;
            }
        } catch (error) {
            console.error('Failed to load categories:', error);
        }
    }

    // Function to fetch and display data
    async function fetchData() {
        const params = new URLSearchParams({
            search: searchNameInput.value,
            category: searchCategoryInput.value,
            status: statusFilterSelect.value,
            master_only: isMasterFilterActive ? 'true' : 'false'
        });

        updateStatus('Fetching data...');
        try {
            const response = await fetch(`/api/filter_data?${params.toString()}`);
            const result = await response.json();

            if (result.status === 'success') {
                dataTableBody.innerHTML = ''; // Clear existing data
                result.data.forEach(row => {
                    const statusIcon = row.is_working === 1 ? '✅' : (row.is_working === 0 ? '❌' : '⏳');
                    const masterBadge = row.is_master === 1 ? '⭐ ' : '';
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${row.logo ? `<img src="${row.logo}" width="40" height="30" style="object-fit: contain;">` : ''}</td>
                        <td>${masterBadge}${row.name || ''}</td>
                        <td>${row.category || ''}</td>
                        <td style="text-align: center;">${statusIcon}</td>
                        <td><a href="${row.url || '#'}" target="_blank">${row.url || ''}</a></td>
                    `;
                    dataTableBody.appendChild(tr);
                });
                updateStatus(`Showing ${result.displayed_count} of ${result.total_filtered} filtered results.`);
            } else {
                updateStatus(`Error: ${result.message}`, 'error');
                dataTableBody.innerHTML = '<tr><td colspan="5">Error loading data.</td></tr>';
            }
        } catch (error) {
            updateStatus(`Network error: ${error.message}`, 'error');
            dataTableBody.innerHTML = '<tr><td colspan="5">Network error loading data.</td></tr>';
        }
    }

    // Event listener for Sync button
    syncDbBtn.addEventListener('click', async () => {
        syncDbBtn.disabled = true;
        updateStatus('Starting database synchronization. This may take a while...');
        try {
            const response = await fetch('/api/sync_db', { 
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    url: m3uUrlInput.value,
                    validate: validateCheckbox.checked
                })
            });
            const result = await response.json();
            if (result.status === 'success') {
                updateStatus(result.message, 'success');
                await loadCategories(); // Refresh categories after sync
                fetchData(); // Refresh data after sync
            } else {
                updateStatus(`Sync failed: ${result.message}`, 'error');
            }
        } catch (error) {
            updateStatus(`Network error during sync: ${error.message}`, 'error');
        } finally {
            syncDbBtn.disabled = false;
        }
    });

    addToMasterBtn.addEventListener('click', async () => {
        updateStatus('Adding working channels to Master List...');
        const response = await fetch('/api/add_to_master', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                search: searchNameInput.value,
                category: searchCategoryInput.value
            })
        });
        const result = await response.json();
        updateStatus(result.message, result.status);
        fetchData();
    });

    removeFromMasterBtn.addEventListener('click', async () => {
        updateStatus('Removing channels from Master List...');
        const response = await fetch('/api/remove_from_master', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                search: searchNameInput.value,
                category: searchCategoryInput.value
            })
        });
        const result = await response.json();
        updateStatus(result.message, result.status);
        fetchData();
    });

    async function switchTab(active) {
        isMasterFilterActive = active;
        baseTabBtn.classList.toggle('active', !active);
        masterTabBtn.classList.toggle('active', active);
        
        // Toggle bulk action buttons
        addToMasterBtn.style.display = active ? 'none' : 'inline-block';
        removeFromMasterBtn.style.display = active ? 'inline-block' : 'none';
        exportMasterM3uBtn.style.display = active ? 'inline-block' : 'none';
        
        await loadCategories();
        fetchData();
    }

    baseTabBtn.addEventListener('click', () => switchTab(false));
    masterTabBtn.addEventListener('click', () => switchTab(true));

    // Event listeners for filters with debounce
    const triggerSearch = () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(fetchData, 300); // Debounce for 300ms
    };

    searchNameInput.addEventListener('input', triggerSearch);
    searchCategoryInput.addEventListener('change', fetchData);
    statusFilterSelect.addEventListener('change', fetchData);

    // Event listener for Export CSV button
    exportCsvBtn.addEventListener('click', () => {
        const params = new URLSearchParams({
            search: searchNameInput.value,
            category: searchCategoryInput.value
        });
        const exportUrl = `/api/export_csv?${params.toString()}`;
        window.open(exportUrl, '_blank'); // Open in new tab to trigger download
        updateStatus('Export initiated. Check your downloads.', 'info');
    });

    // Event listener for M3U Export button
    exportMasterM3uBtn.addEventListener('click', () => {
        const exportUrl = `/api/export_m3u`;
        window.open(exportUrl, '_blank');
        updateStatus('M3U Export initiated.', 'info');
    });

    // Initial data load when the page loads
    loadCategories();
    fetchData();
});