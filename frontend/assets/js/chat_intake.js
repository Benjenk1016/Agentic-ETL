// This runs after the whole page loads
document.addEventListener('DOMContentLoaded', () => {

	// Getting stuff from the page so we can use it in the code
	const input = document.getElementById('user_input'); // text box
	const sendBtn = document.querySelector('.chat-input button'); // send button
	const messages = document.querySelector('.chat-messages'); // chat area
	const fileInput = document.getElementById('file_input'); // hidden file upload input

	// This keeps track of the last file the user uploaded
	window.lastUploadedFile = null;

	// Debug mode - shows detailed logs and stderr (disabled by default)
	window.debugMode = localStorage.getItem('agentic-debug-mode') === 'true';
	
	// State for analyze command (two-file upload flow)
	let analyzeState = null; // { inputFile: File, configFile: File | null }
	const SHOW_SMART_UPDATE_SIMILARITY = false; // Similarity output kept optional for temporary diagnostics.
	let extractColumnsPending = false;
	let analyzeConfigUploadPending = false;
	let configDirUploadPending = false;
	let pendingPreparedFilesAfterConfigUpload = null;
	let oneDriveAuthSessionId = null;

	function getSavedAnalyzeConfigMeta() {
		try {
			const raw = localStorage.getItem('analyze_saved_config');
			if (!raw) return null;
			const parsed = JSON.parse(raw);
			if (!parsed || !parsed.name || !parsed.indexedDbId) return null;
			return parsed;
		} catch {
			return null;
		}
	}

	function setSavedAnalyzeConfigMeta(name, indexedDbId) {
		localStorage.setItem('analyze_saved_config', JSON.stringify({ name, indexedDbId }));
	}

	function promptAnalyzeConfigChoice(savedName, onUseSaved, onChooseDifferent) {
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '8px';

		const text = document.createElement('div');
		text.textContent = `Use saved config "${savedName}"?`;
		msg.appendChild(text);

		const buttonRow = document.createElement('div');
		buttonRow.style.display = 'flex';
		buttonRow.style.gap = '8px';

		const yesBtn = document.createElement('button');
		yesBtn.type = 'button';
		yesBtn.textContent = 'Yes, use saved config';
		yesBtn.classList.add('prompt-button');
		yesBtn.onclick = async () => {
			msg.remove();
			await onUseSaved();
		};

		const noBtn = document.createElement('button');
		noBtn.type = 'button';
		noBtn.textContent = 'No, choose another config';
		noBtn.classList.add('prompt-button');
		noBtn.onclick = () => {
			msg.remove();
			onChooseDifferent();
		};

		buttonRow.appendChild(yesBtn);
		buttonRow.appendChild(noBtn);
		msg.appendChild(buttonRow);

		messages.appendChild(msg);
		scrollMessagesToBottom();
	}

	function promptChooseConfigFile() {
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '8px';
		const text = document.createElement('div');
		text.textContent = '✓ Input file selected. Now click below to choose the config file...';
		msg.appendChild(text);
		const btn = document.createElement('button');
		btn.type = 'button';
		btn.textContent = 'Choose config file';
		btn.style.padding = '6px 14px';
		btn.style.backgroundColor = '#1a73e8';
		btn.style.color = 'white';
		btn.style.border = 'none';
		btn.style.borderRadius = '4px';
		btn.style.cursor = 'pointer';
		btn.style.fontSize = '14px';
		btn.style.alignSelf = 'flex-start';
		btn.onclick = () => {
			msg.remove();
			fileInput.click();
		};
		msg.appendChild(btn);
		messages.appendChild(msg);
		scrollMessagesToBottom();
	}

	function promptChooseConfigFileForDir(contextType = 'default') {
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '8px';
		
		const text = document.createElement('div');
		if (contextType === 'guided_queue') {
			text.textContent = 'No config file found in data/config directory. Provide one now to continue:';
		} else if (contextType === 'analyze') {
			text.textContent = 'No config file found in data/config directory. Provide one now:';
		} else {
			text.textContent = 'Choose a config file from your computer to upload to data/config:';
		}
		msg.appendChild(text);
		
		const btn = document.createElement('button');
		btn.type = 'button';
		btn.textContent = 'Select Config File';
		btn.classList.add('prompt-button');
		btn.style.alignSelf = 'flex-start';
		btn.onclick = () => {
			// File selection happens within click handler - maintains user interaction context
			if (contextType === 'analyze') {
				analyzeConfigUploadPending = true;
			} else {
				configDirUploadPending = true;
			}
			fileInput.accept = '.csv,.xlsx,.xls,.xlsm';
			fileInput.value = '';
			fileInput.click();
		};
		msg.appendChild(btn);
		messages.appendChild(msg);
		scrollMessagesToBottom();
	}

	function createLiveStatusMessage(initialText) {
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		const text = document.createElement('div');
		text.textContent = initialText;
		msg.appendChild(text);
		messages.appendChild(msg);
		scrollMessagesToBottom();

		return (nextText) => {
			text.textContent = nextText;
		};
	}

	// Creates a live message bubble that supports appending text chunks (used for streaming output).
	function addLiveMessage(initialText, role) {
		const msg = document.createElement('div');
		msg.classList.add('message', role || 'agent');
		const text = document.createElement('div');
		text.style.whiteSpace = 'pre-wrap';
		text.textContent = initialText;
		msg.appendChild(text);
		messages.appendChild(msg);
		scrollMessagesToBottom();
		return {
			element: text,
			appendText(chunk) {
				text.textContent += chunk;
				scrollMessagesToBottom();
			},
		};
	}

	// Opens (or creates) a small database in the browser
	function openFilesDB() {
		return new Promise((resolve, reject) => {
			const req = indexedDB.open('agentic-files', 1);

			// If database doesn't exist yet, create it
			req.onupgradeneeded = (ev) => {
				const db = ev.target.result;
				if (!db.objectStoreNames.contains('files')) {
					db.createObjectStore('files', { keyPath: 'id', autoIncrement: true });
				}
			};

			req.onsuccess = () => resolve(req.result);
			req.onerror = () => reject(req.error);
		});
	}

	// Saves Excel file data into the browser database
	function saveFileToIndexedDB(name, arrayBuffer) {
		return new Promise(async (resolve, reject) => {
			try {
					const db = await openFilesDB();
					const tx = db.transaction('files', 'readwrite');
					const store = tx.objectStore('files');

					// Turn file data into a blob (basically a file-like object)
					const blob = new Blob([arrayBuffer], { type: 'application/octet-stream' });
					const req = store.add({ name, blob });

					req.onsuccess = (ev) => resolve(ev.target.result);
					req.onerror = () => reject(req.error);
			} catch (err) {
				reject(err);
			}
		});
	}

	// Gets a file back out of the browser database
	function getFileFromIndexedDB(id) {
		return new Promise(async (resolve, reject) => {
			try {
					const db = await openFilesDB();
					const tx = db.transaction('files', 'readonly');
					const store = tx.objectStore('files');
					const req = store.get(id);

					req.onsuccess = () => resolve(req.result && req.result.blob);
					req.onerror = () => reject(req.error);
			} catch (err) {
				reject(err);
			}
		});
	}

	// Helper function in case we need to grab the Excel file later
	window.getExcelFileFromDb = async (id) => {
		const blob = await getFileFromIndexedDB(id);
		if (!blob) return null;
		return await blob.arrayBuffer();
	};

	/* ------------------ File Upload ------------------ */

	// When user selects a file : read file and save raw contents to localStorage
	if (fileInput) {
		fileInput.addEventListener('change', async (ev) => {
			const file = ev.target.files && ev.target.files[0];
			if (!file) return;
			addMessage('Selected file: ' + file.name, 'agent');

			if (configDirUploadPending) {
				configDirUploadPending = false;
				fileInput.accept = '.csv,.xlsx,.xls,.xlsm';
				fileInput.value = '';
				const uploaded = await uploadConfigToConfigDir(file);
				if (uploaded) {
					addMessage(`Config uploaded to data/config directory and saved there: ${uploaded.config_name}`, 'agent');
					if (Array.isArray(pendingPreparedFilesAfterConfigUpload) && pendingPreparedFilesAfterConfigUpload.length > 0) {
						const queuedFiles = pendingPreparedFilesAfterConfigUpload;
						pendingPreparedFilesAfterConfigUpload = null;
						addConfirmation(
							`Start guided AI analysis now for ${queuedFiles.length} prepared file(s)?`,
							async () => {
								await runPreparedAnalysisQueue(queuedFiles, 0, uploaded.config_name);
							},
							() => {
								addMessage('Skipped guided AI analysis. You can still run "analyze" later.', 'agent');
							}
						);
					} else {
						addMessage('Running analyze again with config from data/config...', 'agent');
						await analyzeUsingServerFiles(uploaded.config_name, true);
					}
				}
				return;
			}

			if (analyzeConfigUploadPending) {
				analyzeConfigUploadPending = false;
				fileInput.accept = '.csv,.xlsx,.xls,.xlsm';
				fileInput.value = '';
				const uploaded = await uploadConfigToConfigDir(file);
				if (uploaded) {
					addMessage('Config uploaded to data/config directory. Running analyze again...', 'agent');
					await analyzeUsingServerFiles(uploaded.config_name, true);
				}
				return;
			}

			// Check if we're in analyze mode (two-file upload)
			if (analyzeState !== null) {
				if (analyzeState.inputFile === null) {
					// First file (input)
					analyzeState.inputFile = file;
					fileInput.value = '';

					const savedConfig = getSavedAnalyzeConfigMeta();
					if (savedConfig) {
						promptAnalyzeConfigChoice(
							savedConfig.name,
							async () => {
								try {
									const savedBlob = await getFileFromIndexedDB(savedConfig.indexedDbId);
									if (!savedBlob) {
										addMessage('Saved config is no longer available. Please choose a config file.', 'agent');
										promptChooseConfigFile();
										return;
									}
									const savedFile = new File([savedBlob], savedConfig.name, {
										type: savedBlob.type || 'application/octet-stream',
									});
									addMessage('Using saved config: ' + savedConfig.name, 'agent');
									await analyzeFiles(analyzeState.inputFile, savedFile);
									analyzeState = null;
								} catch (err) {
									addMessage('Could not load saved config. Please choose a config file.', 'agent');
									promptChooseConfigFile();
								}
							},
							() => {
								promptChooseConfigFile();
							}
						);
					} else {
						promptChooseConfigFile();
					}
					return;
				} else {
					// Second file (config)
					analyzeState.configFile = file;
					fileInput.value = '';

					const runAnalyzeNow = async () => {
						addMessage('✓ Config file selected. Sending to Ollama for analysis...', 'agent');
						await analyzeFiles(analyzeState.inputFile, analyzeState.configFile);
						analyzeState = null;
					};

					// addConfirmation(
					// 	'Would you like to save this config file for future runs?',
					// 	// async () => {
					// 	// 	try {
					// 	// 		const configBuffer = await file.arrayBuffer();
					// 	// 		const configId = await saveFileToIndexedDB(file.name, configBuffer);
					// 	// 		setSavedAnalyzeConfigMeta(file.name, configId);
					// 	// 		addMessage('Saved config for future runs.', 'agent');
					// 	// 	} catch (err) {
					// 	// 		addMessage('Could not save config, but analysis will continue.', 'agent');
					// 	// 	}
					// 	// 	await runAnalyzeNow();
					// 	// },
					// 	async () => {
					// 		addMessage('Using config once (not saved).', 'agent');
					// 		await runAnalyzeNow();
					// 	},
					// 	async () => {
					// 		addMessage('Using config once (not saved).', 'agent');
					// 		await runAnalyzeNow();
					// 	}
					// );
					// return;
				}
			}

			if (extractColumnsPending) {
				extractColumnsPending = false;
				fileInput.accept = '.csv,.xlsx,.xls,.xlsm';
				fileInput.value = '';
				await extractColumnsFromSelectedFile(file);
				return;
			}
			
			try {
				// If it's a CSV file
				if (file.name.toLowerCase().endsWith('.csv')) {
					const text = await file.text();

					// Save file info in memory and localStorage
					window.lastUploadedFile = { name: file.name, type: 'csv', content: text };
					localStorage.setItem('last_uploaded_file', JSON.stringify(window.lastUploadedFile));
					// Show preview
					parseAndRenderFromText(text, file.name);
				} 
				else {
					// If it's Excel
					const ab = await file.arrayBuffer();
					// Save binary to IndexedDB (safer for larger files)
					const id = await saveFileToIndexedDB(file.name, ab);
					window.lastUploadedFile = { name: file.name, type: 'excel', indexedDbId: id };
					localStorage.setItem('last_uploaded_file', JSON.stringify(window.lastUploadedFile));
					parseAndRenderFromArrayBuffer(ab, file.name);
				}

				// Send file to backend server through API for processing (validation + transformation)
				await uploadAndProcess(file);

			} catch (err) {
				addMessage('Problem with file: ' + err.message, 'agent');
			}
			
			// Reset file input for next selection
			fileInput.value = '';
		});
	}

	/* ------------------ Upload (Manual Processing) ------------------ */

	function summarizeSheetChanges(sheetChanges) {
		if (!sheetChanges || typeof sheetChanges !== 'object') {
			return 'No sheet-level details available.';
		}

		const added = Array.isArray(sheetChanges.added_sheets) ? sheetChanges.added_sheets : [];
		const removed = Array.isArray(sheetChanges.removed_sheets) ? sheetChanges.removed_sheets : [];
		const updated = Array.isArray(sheetChanges.updated_sheets) ? sheetChanges.updated_sheets : [];

		const parts = [];
		parts.push(`Sheets added: ${added.length}${added.length ? ` (${added.slice(0, 4).join(', ')})` : ''}`);
		parts.push(`Sheets removed: ${removed.length}${removed.length ? ` (${removed.slice(0, 4).join(', ')})` : ''}`);
		parts.push(`Sheets updated: ${updated.length}${updated.length ? ` (${updated.slice(0, 4).join(', ')})` : ''}`);
		return parts.join('\n');
	}

	function summarizeRowChanges(payload) {
		const addedRows = Array.isArray(payload.added_rows) ? payload.added_rows : [];
		const removedRows = Array.isArray(payload.removed_rows) ? payload.removed_rows : [];
		const updatedRows = Array.isArray(payload.updated_rows) ? payload.updated_rows : [];
		const valueChanges = Array.isArray(payload.value_changes) ? payload.value_changes : [];

		const lines = [];
		lines.push(`Rows added: ${addedRows.length}`);
		lines.push(`Rows removed: ${removedRows.length}`);
		lines.push(`Rows updated: ${updatedRows.length}`);
		lines.push(`Cell updates: ${valueChanges.length}`);

		if (valueChanges.length > 0) {
			const samples = valueChanges.slice(0, 3).map((change) => {
				const sheet = change.sheet ? `${change.sheet} ` : '';
				return `- ${sheet}row ${change.row}, ${change.column}: "${change.old}" -> "${change.new}"`;
			});
			lines.push('Sample changes:');
			lines.push(...samples);
		}

		return lines.join('\n');
	}

	function isNewFileChangeImpact(changeImpact, changeDetection) {
		const impactLevel = String(changeImpact?.level || '').toLowerCase();
		if (impactLevel === 'new') return true;

		const detectionStatus = String(changeDetection?.status || '').toLowerCase();
		return detectionStatus === 'first_version';
	}

	async function runAutoChangeDetectionForUpload(fileName) {
		try {
			const response = await fetch('http://localhost:5001/check_file_changes', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ file_name: fileName, update_baseline: false }),
			});

			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				addMessage('Change detection failed: ' + (payload.message || 'Unknown error'), 'agent');
				return;
			}

			if (payload.status === 'first_version') {
				addMessage('Change detection: first saved version created for this file.', 'agent');
				return;
			}

			if (payload.status === 'no_change') {
				addMessage('Change detection: no differences from the saved baseline.', 'agent');
				return;
			}

			if (payload.status === 'changed') {
				const sheetSummary = summarizeSheetChanges(payload.sheet_changes);
				const rowSummary = summarizeRowChanges(payload);
				addMessage('Change detection summary:\n' + sheetSummary + '\n' + rowSummary, 'agent');
				return;
			}

			addMessage('Change detection complete: ' + (payload.message || payload.status), 'agent');
		} catch (err) {
			addMessage('Change detection error: ' + err.message, 'agent');
		}
	}

	// Uploads file to the backend input folder without auto-running scripts.
	async function uploadAndProcess(file, options = {}) {
		const showRunPrompt = options.showRunPrompt !== false;
		try {
			const formData = new FormData();
			formData.append('file', file);

			const response = await fetch('http://localhost:5001/upload_and_process', {
				method: 'POST',
				body: formData,
			});

			if (!response.ok) {
				addMessage('Upload failed.', 'agent');
				return false;
			}

			const payload = await response.json();

			if (payload.status === 'error') {
				addMessage('Upload failed: ' + payload.message, 'agent');
				return false;
			}

			addMessage(`✓ File chosen and uploaded: ${file.name}`, 'agent');
			addMessage('File is now in project input. Run "run file processing" when you are ready.', 'agent');

			if (payload.change_result) {
				// Compatibility path for upload endpoint versions that return direct change details.
				showChangeResults(payload.change_result);
				if (payload.change_result.status === 'changed' || payload.change_result.status === 'first_version') {
					addMessage('This version is now saved as the new comparison baseline.', 'agent');
				}
			} else if (payload.baseline_created === true || payload.baseline_status === 'first_version') {
				addMessage('Change detection: first saved version created for this file.', 'agent');
			} else {
				// Automatically run change detection after upload for command 1.
				await runAutoChangeDetectionForUpload(file.name);
			}

			if (!showRunPrompt) {
				// Preserve silent uploads used by analyze config flow.
				return true;
			}

			return true;

		} catch (err) {
			addMessage('Server problem: ' + err.message, 'agent');
			return false;
		}
	}

	/* ------------------ Parsing + Table Preview ------------------ */

	// Builds a small preview table (first 10 rows). Uses SheetJS
	function renderTableFromRows(rows, title) {
		const preview = document.getElementById('file-preview');
		if (!preview){ 
			//error message for console if preview element is missing
			console.error('renderTableFromRows: #file-preview element not found');
			return;
		}

		preview.innerHTML = '';
		// Create header with title and close button
		const header = document.createElement('div');
		header.classList.add('preview-header');


		// Title of the preview
		const heading = document.createElement('div');
		heading.textContent = title || 'File preview';
		heading.style.fontWeight = '600';
		heading.style.margin = '8px 0';
		// If there are no rows, show a message instead of the table
		if (!rows || rows.length === 0) {
			preview.innerHTML = 'No data found.';
			return;
		}
		// Close button to clear the preview
		const closeBtn = document.createElement('button');
		closeBtn.type = 'button';
		closeBtn.classList.add('preview-close');
		closeBtn.setAttribute('aria-label', 'Close preview');
		closeBtn.textContent = '×';
		closeBtn.addEventListener('click', () => {preview.innerHTML = '';});

		// Add header and table to preview
		header.appendChild(heading);
		header.appendChild(closeBtn);
		preview.appendChild(header);

		// Create table element and populate with data
		const table = document.createElement('table');
		table.style.borderCollapse = 'collapse';
		//style table to fit nicely within the preview area with some max width and centered alignment
		table.style.maxWidth = '814.46px';
		table.style.margin = '0 auto';
		table.style.marginBottom = '25px';
		const headers = rows[0];
		//add column headers
		const thead = document.createElement('thead');
		const headerRow = document.createElement('tr');
		for (let h of headers) {
			const th = document.createElement('th');
			th.textContent = h ?? '';
			th.style.border = '1px solid #ccc';
			headerRow.appendChild(th);
		}
		thead.appendChild(headerRow);
		table.appendChild(thead);

		//add up to 10 rows of data
		const tbody = document.createElement('tbody');
		const maxRows = Math.min(10, rows.length - 1);
		for (let r = 0; r < maxRows; r++) {
				const row = document.createElement('tr');
				const rowData = rows[r + 1] || [];
				for (let c = 0; c < headers.length; c++) {
					const td = document.createElement('td');
					td.textContent = rowData[c] ?? '';
					td.style.border = '1px solid #eee';
					row.appendChild(td);
				}
			tbody.appendChild(row);
		}
		table.appendChild(tbody);
		preview.appendChild(table);
		preview.style.display = 'block';
	}

	// Reads Excel file
	function parseAndRenderFromArrayBuffer(ab, name) {
		try {
			//console debug to indicate we're parsing the Excel file 
			console.debug('Parsing Excel arrayBuffer for', name);

			
			const wb = XLSX.read(ab, { type: 'array' });
			if (!wb || !wb.SheetNames.length) {
				/*console debug to indicate no sheets were found in the Excel file */console.error('No sheets found in workbook', wb);
				addMessage('No data found in Excel file.', 'agent');
				return;
			}

			const sheet = wb.Sheets[wb.SheetNames[0]];
			const rows = XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false });
			renderTableFromRows(rows, name);

		} catch (err) {
			/*console debug to show error details when parsing Excel file fails */console.error('Error parsing Excel file', err);
			addMessage('Cannot read Excel file.', 'agent');
		}
	}

	// Reads CSV/text file
	function parseAndRenderFromText(text, name) {
		try {
			const wb = XLSX.read(text, { type: 'string' });
			if (!wb || !wb.SheetNames.length) {
				addMessage('No data found.', 'agent');
				return;
			}

			const sheet = wb.Sheets[wb.SheetNames[0]];
			const rows = XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false });
			renderTableFromRows(rows, name);
		} catch (err) {
			addMessage('Cannot read file.', 'agent');
		}
	}

	/* ------------------ Chat + Commands ------------------ */

	// Adds a message bubble to chat
	function scrollMessagesToBottom() {
		if (!messages) return;
		// Defer until layout settles so scrollHeight is accurate.
		requestAnimationFrame(() => {
			messages.scrollTop = messages.scrollHeight;
		});
	}

	function addMessage(content, sender) {
		const msg = document.createElement('div');
		msg.classList.add('message', sender);
		const normalized = String(content).replace(/^\s*\n+/, '');
		msg.textContent = normalized;
		messages.appendChild(msg);

		// Always scroll to bottom
		scrollMessagesToBottom();
	}

	// Show change results from API
	function showChangeResults(payload) {
		if (!payload) return;

		if (payload.status === 'first_version') {
			addMessage('This is the first saved version of the file.', 'agent');
			addMessage('Upload a changed version with the same file name later to compare it.', 'agent');
			return;
		}

		if (payload.status === 'no_change') {
			addMessage('No changes found in the file.', 'agent');
			return;
		}

		if (payload.status === 'changed') {
			let resultText = 'Changes found in ' + payload.file;

			if (payload.columns_added && payload.columns_added.length > 0) {
				resultText += '\nColumns added: ' + payload.columns_added.join(', ');
			}

			if (payload.columns_removed && payload.columns_removed.length > 0) {
				resultText += '\nColumns removed: ' + payload.columns_removed.join(', ');
			}

			resultText += '\nOld row count: ' + payload.row_count_old;
			resultText += '\nNew row count: ' + payload.row_count_new;

			if (payload.value_changes && payload.value_changes.length > 0) {
				resultText += '\nValue changes:';

				payload.value_changes.slice(0, 20).forEach(change => {
					resultText += '\n- Row ' + change.row +
						', Column "' + change.column +
						'": "' + change.old +
						'" -> "' + change.new + '"';
				});

				if (payload.value_changes.length > 20) {
					resultText += '\n...and ' + (payload.value_changes.length - 20) + ' more changes.';
				}
			} else {
				resultText += '\nNo cell value changes found.';
			}

			addMessage(resultText, 'agent');
			return;
		}

		if (payload.status === 'error') {
			addMessage('Could not check changes: ' + payload.message, 'agent');
			return;
		}

		addMessage('Finished checking file changes.', 'agent');
	}

	// trigger to open file dialog when user types "input" command
	function triggerFileInput() {
		if (!fileInput) {
			return;
		}
		//open native file dialog to select CSV or Excel file
		fileInput.click();
		addMessage('Choose a CSV or Excel file.', 'agent');
	}

	// Adds a confirmation prompt with yes/no buttons
	function addConfirmation(text, onYes, onNo) {
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '8px';

		const textEl = document.createElement('div');
		textEl.textContent = text;
		msg.appendChild(textEl);

		const buttonContainer = document.createElement('div');
		buttonContainer.classList.add('message-actions');

		const yesBtn = document.createElement('button');
		yesBtn.type = 'button';
		yesBtn.textContent = 'Yes';
		yesBtn.classList.add('chat-action-button');
		yesBtn.onclick = () => {
			msg.remove();
			onYes();
		};

		const noBtn = document.createElement('button');
		noBtn.type = 'button';
		noBtn.textContent = 'No';
		noBtn.classList.add('chat-action-button', 'secondary');
		noBtn.onclick = () => {
			msg.remove();
			if (onNo) onNo();
		};

		buttonContainer.appendChild(yesBtn);
		buttonContainer.appendChild(noBtn);
		msg.appendChild(buttonContainer);

		messages.appendChild(msg);
		scrollMessagesToBottom();
	}

	// Adds a confirmation prompt with three explicit options.
	function addThreeOptionConfirmation(text, options) {
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '8px';

		const textEl = document.createElement('div');
		textEl.textContent = text;
		msg.appendChild(textEl);

		const buttonContainer = document.createElement('div');
		buttonContainer.classList.add('message-actions');

		const yesBtn = document.createElement('button');
		yesBtn.type = 'button';
		yesBtn.textContent = options.yesLabel || 'Yes';
		yesBtn.classList.add('chat-action-button', options.yesClass || 'variant-yes');
		yesBtn.onclick = () => {
			msg.remove();
			if (options.onYes) options.onYes();
		};

		const skipBtn = document.createElement('button');
		skipBtn.type = 'button';
		skipBtn.textContent = options.skipLabel || 'Skip';
		skipBtn.classList.add('chat-action-button', options.skipClass || 'variant-skip');
		skipBtn.onclick = () => {
			msg.remove();
			if (options.onSkip) options.onSkip();
		};

		const cancelBtn = document.createElement('button');
		cancelBtn.type = 'button';
		cancelBtn.textContent = options.cancelLabel || 'Cancel';
		cancelBtn.classList.add('chat-action-button', options.cancelClass || 'variant-cancel');
		cancelBtn.onclick = () => {
			msg.remove();
			if (options.onCancel) options.onCancel();
		};

		let extraBtn = null;
		if (options.extraLabel && options.onExtra) {
			extraBtn = document.createElement('button');
			extraBtn.type = 'button';
			extraBtn.textContent = options.extraLabel;
			extraBtn.classList.add('chat-action-button', options.extraClass || 'variant-auto');
			extraBtn.onclick = () => {
				msg.remove();
				options.onExtra();
			};
		}

		let extra2Btn = null;
		if (options.extra2Label && options.onExtra2) {
			extra2Btn = document.createElement('button');
			extra2Btn.type = 'button';
			extra2Btn.textContent = options.extra2Label;
			extra2Btn.classList.add('chat-action-button', options.extra2Class || 'variant-auto');
			extra2Btn.onclick = () => {
				msg.remove();
				options.onExtra2();
			};
		}

		let extra3Btn = null;
		if (options.extra3Label && options.onExtra3) {
			extra3Btn = document.createElement('button');
			extra3Btn.type = 'button';
			extra3Btn.textContent = options.extra3Label;
			extra3Btn.classList.add('chat-action-button', options.extra3Class || 'variant-auto');
			extra3Btn.onclick = () => {
				msg.remove();
				options.onExtra3();
			};
		}

		buttonContainer.appendChild(yesBtn);
		buttonContainer.appendChild(skipBtn);
		if (options.extraLast) {
			buttonContainer.appendChild(cancelBtn);
			if (extraBtn) buttonContainer.appendChild(extraBtn);
			if (extra2Btn) buttonContainer.appendChild(extra2Btn);
			if (extra3Btn) buttonContainer.appendChild(extra3Btn);
		} else {
			if (extraBtn) buttonContainer.appendChild(extraBtn);
			if (extra2Btn) buttonContainer.appendChild(extra2Btn);
			if (extra3Btn) buttonContainer.appendChild(extra3Btn);
			buttonContainer.appendChild(cancelBtn);
		}
		msg.appendChild(buttonContainer);

		messages.appendChild(msg);
		scrollMessagesToBottom();
	}

	// Cleans up backend logs so they look nicer
	function formatLog(text) {
		return text.replace(/\s*\[(Info|Warning|Error)\]/g, '\n[$1]').trim();
	}


	// Calls backend API
	async function callApi(path, startMessage, showMessageAlways = false) {
		addMessage(startMessage, 'agent');

		try {
			const response = await fetch('http://localhost:5001' + path, { 
				method: 'POST' 
			});
			const payload = await response.json();

			if (!response.ok) {
				addMessage('⚠ Server error.', 'agent');
				return;
			}

			// Show completion message if debug mode OR if showMessageAlways is true
			if ((window.debugMode || showMessageAlways) && payload.message) {
				addMessage(payload.message, 'agent');
			}

			// Show debug logs if enabled
			if (window.debugMode) {
				if (payload.stdout) {
					addMessage('[DEBUG] stdout:\n' + formatLog(payload.stdout), 'agent');
				}
				if (payload.stderr) {
					addMessage('[DEBUG] stderr:\n' + formatLog(payload.stderr), 'agent');
				}
			} else if (payload.stderr) {
				// Always show errors even without debug mode
				addMessage('⚠ Issues encountered:\n' + formatLog(payload.stderr), 'agent');
			}

		} catch (err) {
			addMessage('⚠ Server problem: ' + err.message, 'agent');
		}
	}

	// Calls backend API silently (no intermediate messages)
	async function callApiSilent(path) {
		try {
			const response = await fetch('http://localhost:5001' + path, { method: 'POST' });
			const payload = await response.json();

			if (!response.ok) {
				/*console debug to show error details when API call fails */console.error('API call failed', { path, status: response.status, payload });
				return { success: false, error: 'Server error' };
			}

			if (payload.stderr && !window.debugMode) {
				return { success: false, error: payload.stderr };
			}

			return { success: true, payload };
		} catch (err) {
			return { success: false, error: err.message };
		}
	}

	async function analyzePreparedInputFile(inputFileName, options = {}) {
		const {
			confirmAiAnalysis = false,
			configFileName = null,
			saveDefaultConfig = false,
			timeoutMs = 180000,
			prepareOnly = false,
			preparedPrompt = null,
			preparedPromptFile = null,
			preparedInputFileInfo = null,
			preparedConfigFileInfo = null,
			preparedInputFileName = null,
			preparedInputColumnData = null,
			preparedInputRowData = null,
			preparedConfigColumnData = null,
			preparedChangeStatus = null,
			ignoreSavedDefaultConfig = false,
		} = options;
		const requestBody = {
			input_file_name: inputFileName,
			config_file_name: configFileName,
			save_default_config: saveDefaultConfig,
			confirm_ai_analysis: confirmAiAnalysis,
			prepare_only: prepareOnly,
			prepared_prompt: preparedPrompt,
			prepared_prompt_file: preparedPromptFile,
			prepared_input_file_info: preparedInputFileInfo,
			prepared_config_file_info: preparedConfigFileInfo,
			prepared_input_file_name: preparedInputFileName,
			prepared_input_column_data: preparedInputColumnData,
			prepared_input_row_data: preparedInputRowData,
			prepared_config_column_data: preparedConfigColumnData,
			prepared_change_status: preparedChangeStatus,
			ignore_saved_default_config: ignoreSavedDefaultConfig,
		};
		let timeoutHandle;
		const controller = new AbortController();
		try {
			timeoutHandle = setTimeout(() => controller.abort(), timeoutMs);
			const response = await fetch('http://localhost:5001/analyze-excel_files-from-input', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(requestBody),
				signal: controller.signal,
			});
			const payload = await response.json().catch(() => ({ status: 'error', message: 'Invalid server response.' }));
			if (!response.ok) {
				return {
					status: 'error',
					message: payload?.message || 'Server error during analyze.',
					payload,
				};
			}
			if (payload && payload.error && !payload.status) {
				return {
					status: 'error',
					message: String(payload.error),
					payload,
				};
			}
			return payload;
		} catch (err) {
			if (err && err.name === 'AbortError') {
				return {
					status: 'error',
					message: `Analyze request timed out after ${Math.floor(timeoutMs / 1000)} seconds. The queue is still active; retry this file or skip to the next one.`,
				};
			}
			return {
				status: 'error',
				message: `Analyze request failed: ${err.message}`,
			};
		} finally {
			clearTimeout(timeoutHandle);
		}
	}

	async function fetchAnalyzeConfigOptions() {
		try {
			const response = await fetch('http://localhost:5001/analyze/config-options');
			if (!response.ok) return null;
			return await response.json();
		} catch (err) {
			return null;
		}
	}

	async function uploadConfigToConfigDir(file) {
		try {
			const formData = new FormData();
			formData.append('file', file);
			const response = await fetch('http://localhost:5001/upload_config', {
				method: 'POST',
				body: formData,
			});
			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				addMessage('Config upload failed: ' + (payload.message || 'Unknown error'), 'agent');
				return null;
			}
			return payload;
		} catch (err) {
			addMessage('Config upload failed: ' + err.message, 'agent');
			return null;
		}
	}

	async function showLlmStartupDiagnostics() {
		try {
			const diagResponse = await fetch('http://localhost:5001/llm/startup-diagnostics');
			if (diagResponse.ok) {
				const diag = await diagResponse.json();
				const elapsed = diag.elapsed_sec !== null && diag.elapsed_sec !== undefined ? ` ${diag.elapsed_sec}s` : '';
				addMessage(`LLM: ${diag.model || 'unknown'} | warmup: ${diag.status || 'unknown'}${elapsed}`, 'agent');
				await flushChatUi();
			}
		} catch (err) {
			// Diagnostics are optional; continue.
		}
	}

	async function runConfirmedPreparedAnalyze(inputFileName, selectedConfigName, promptPreviewWidget = null, prePreparedPayload = null, skipPromptDialog = false) {
		let updateStatusLine = null;
		let elapsedTicker = null;
		let liveSummary = null;
		try {
			const prepared = prePreparedPayload || await analyzePreparedInputFile(inputFileName, {
				confirmAiAnalysis: true,
				configFileName: selectedConfigName,
				prepareOnly: true,
			});
			if (prepared.status !== 'prepared') {
				return prepared;
			}

			// Remove the loading placeholder (if any) before showing the editable prompt.
			if (promptPreviewWidget && typeof promptPreviewWidget.remove === 'function') {
				promptPreviewWidget.remove();
			}
			await flushChatUi();

			// In auto mode, skip the prompt edit dialog and use the prepared prompt directly
			let editedPrompt = prepared.prompt || '';
			if (!skipPromptDialog) {
				// Let the client review or edit the prompt before sending to Ollama.
				editedPrompt = await addEditablePromptPreview(prepared.prompt || '', {
					title: `Review & Edit Prompt for ${inputFileName}`,
				});
				if (editedPrompt === null) {
					addMessage('Analysis cancelled.', 'agent');
					return { status: 'cancelled' };
				}
			}

			await showLlmStartupDiagnostics();
			const startedAt = Date.now();
			updateStatusLine = createLiveStatusMessage('Ollama is processing... elapsed time: 0 seconds');
			await flushChatUi();
			elapsedTicker = setInterval(() => {
				const elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
				updateStatusLine(`Ollama is processing... elapsed time: ${elapsedSeconds} seconds`);
			}, 1000);

			addMessage('AI Analysis Streaming:', 'agent');
			liveSummary = addLiveMessage('', 'agent');
			const result = await streamPreparedAnalyze(
				{
					...prepared,
					prompt: editedPrompt,
					prepared_config_file_name: prepared.prepared_config_file_name || prepared.selected_config || selectedConfigName || '',
				},
				{
					timeoutMs: 180000,
					onChunk: (chunk) => { liveSummary.appendText(chunk); },
				},
			);
			if (result) {
				result.prompt_preview_rendered = true;
				result.stream_rendered = true;
				if (!result.prompt) result.prompt = editedPrompt;
				if (!result.prepared_config_file_name && prepared.prepared_config_file_name) {
					result.prepared_config_file_name = prepared.prepared_config_file_name;
				}
				if (!result.prepared_config_file_name && prepared.selected_config) {
					result.prepared_config_file_name = prepared.selected_config;
				}
				if (!result.input_column_data && prepared.input_column_data) {
					result.input_column_data = prepared.input_column_data;
				}
				if (!result.input_row_data && prepared.input_row_data) {
					result.input_row_data = prepared.input_row_data;
				}
				if (!result.config_column_data && prepared.config_column_data) {
					result.config_column_data = prepared.config_column_data;
				}
			}
			return result;
		} finally {
			if (elapsedTicker) clearInterval(elapsedTicker);
		}
	}

	function showGuidedAnalyzeFailure(fileName, rawMessage, positionLabel) {
		const message = String(rawMessage || 'Unknown error');
		const lower = message.toLowerCase();
		const isTimeout = lower.includes('timed out') || lower.includes('timeout');
		const isOllama = lower.includes('ollama');

		addMessage(`Could not analyze ${fileName}: ${message}`, 'agent');

		if (isTimeout && isOllama) {
			addMessage(
				`Queue status: still running. ${fileName} timed out while waiting for Ollama; the next file prompt will continue at ${positionLabel}.`,
				'agent'
			);
			addMessage(
				'Suggestion: keep Ollama open, or use a smaller model / increase OLLAMA_READ_TIMEOUT_SECONDS, then retry timed-out files later.',
				'agent'
			);
			return;
		}

		if (isOllama) {
			addMessage(
				`Queue status: still running. Ollama returned an error for ${fileName}; the next file prompt will continue at ${positionLabel}.`,
				'agent'
			);
			return;
		}

		addMessage(
			`Queue status: still running. The next file prompt will continue at ${positionLabel}.`,
			'agent'
		);
	}

	async function flushChatUi() {
		await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
	}

	async function runPreparedAnalysisQueue(fileNames, index = 0, selectedConfigName = null) {
		if (!Array.isArray(fileNames) || fileNames.length === 0) {
			addMessage('No prepared files to analyze.', 'agent');
			return;
		}

		if (index >= fileNames.length) {
			addMessage('Finished guided AI analysis queue for prepared files.', 'agent');
			addConfirmation(
				'Would you like to start the AI response processing workflow?',
				async () => {
					await runResponseProcessingWorkflow();
				},
				() => {
					addMessage('Skipped AI response processing workflow. You can run "process responses" later.', 'agent');
				}
			);
			return;
		}

		const fileName = fileNames[index];
		const positionLabel = `${index + 1}/${fileNames.length}`;

		addThreeOptionConfirmation(`Run AI analysis for ${fileName}? (${positionLabel})`, {
			yesLabel: 'Yes',
			skipLabel: 'Skip This File',
			extraLabel: 'Auto-run analysis on all input files',
			cancelLabel: 'Cancel Remaining Queue',
			extraLast: true,
			extraClass: 'variant-auto',
			onYes: async () => {
				const firstPass = await analyzePreparedInputFile(fileName, {
					confirmAiAnalysis: true,
					prepareOnly: true,
					configFileName: selectedConfigName,
					ignoreSavedDefaultConfig: !selectedConfigName,
				});

				if (firstPass.status === 'prepared' || firstPass.status === 'needs_analysis_confirmation') {
					if (firstPass.change_impact && firstPass.change_impact.label) {
						const reason = firstPass.change_impact.reason ? ` (${firstPass.change_impact.reason})` : '';
						addMessage(`Change impact for ${fileName}: ${firstPass.change_impact.label}${reason}`, 'agent');
					}
					if (firstPass.change_detection && !isNewFileChangeImpact(firstPass.change_impact, firstPass.change_detection)) {
						const sheetSummary = summarizeSheetChanges(firstPass.change_detection.sheet_changes || {});
						const rowSummary = summarizeRowChanges(firstPass.change_detection);
						addMessage(`Change summary for ${fileName}:\n${sheetSummary}\n${rowSummary}`, 'agent');
					}
					//addMessage(`Running AI analysis for ${fileName}...`, 'agent');
					const promptPreviewWidget = addPromptPreview(`Preparing prompt for ${fileName}...`, {
						title: 'Prompt Preview (Preparing)',
					});
					await flushChatUi();
					const confirmed = await runConfirmedPreparedAnalyze(fileName, selectedConfigName, promptPreviewWidget, firstPass.status === 'prepared' ? firstPass : null);
					if (confirmed.status === 'ok') {
						renderAnalyzeResult(confirmed);
						addMessage(`Finished analysis for ${fileName}. Moving to next file...`, 'agent');
					} else {
						showGuidedAnalyzeFailure(fileName, confirmed.message || confirmed.status, `${index + 2}/${fileNames.length}`);
					}
					await flushChatUi();
					await runPreparedAnalysisQueue(fileNames, index + 1, selectedConfigName);
					return;
				}

				if (firstPass.status === 'ok') {
					renderAnalyzeResult(firstPass);
					addMessage(`Finished analysis for ${fileName}. Moving to next file...`, 'agent');
					await runPreparedAnalysisQueue(fileNames, index + 1, selectedConfigName);
					return;
				}

				if (firstPass.status === 'needs_config_selection') {
					showConfigSelectionPrompt(
						firstPass.config_options || [],
						async (configName) => {
							addMessage(`Using selected config for guided queue: ${configName}`, 'agent');
							await runPreparedAnalysisQueue(fileNames, index, configName);
						},
						async () => {
							addMessage(
								`Upload a config file, then run 'run file processing' again to resume guided analysis.`,
								'agent'
							);
						}
					);
					return;
				}

				if (firstPass.status === 'needs_config_upload') {
					addMessage(
						`Cannot auto-analyze ${fileName}: no config file found in data/config. Choose one from files and it will be uploaded to data/config and saved there.`,
						'agent'
					);
					pendingPreparedFilesAfterConfigUpload = fileNames.slice(index);
					promptChooseConfigFileForDir('guided_queue');
					return;
				}

				showGuidedAnalyzeFailure(fileName, firstPass.message || firstPass.status || 'Unknown error', `${index + 2}/${fileNames.length}`);
				await runPreparedAnalysisQueue(fileNames, index + 1, selectedConfigName);
			},
			onSkip: async () => {
				addMessage(`Skipped ${fileName}.`, 'agent');
				await runPreparedAnalysisQueue(fileNames, index + 1, selectedConfigName);
			},
			onExtra: async () => {
				addMessage('Auto-run enabled for remaining prepared files.', 'agent');
				await runPreparedAnalysisQueueAuto(fileNames, index, selectedConfigName);
			},
			onCancel: () => {
				addMessage('Cancelled guided AI analysis queue.', 'agent');
			},
		});
	}

	async function runPreparedAnalysisQueueAuto(fileNames, index = 0, selectedConfigName = null) {
		if (!Array.isArray(fileNames) || fileNames.length === 0) {
			addMessage('No prepared files to analyze.', 'agent');
			return;
		}

		if (index >= fileNames.length) {
			addMessage('Finished guided AI analysis queue for prepared files.', 'agent');
			addConfirmation(
				'Would you like to start the AI response processing workflow?',
				async () => {
					await runResponseProcessingWorkflow();
				},
				() => {
					addMessage('Skipped AI response processing workflow. You can run "process responses" later.', 'agent');
				}
			);
			return;
		}

		const fileName = fileNames[index];
		addMessage(`Auto-running analysis for ${fileName} (${index + 1}/${fileNames.length})...`, 'agent');
		await flushChatUi();

		const firstPass = await analyzePreparedInputFile(fileName, {
			confirmAiAnalysis: true,
			prepareOnly: true,
			configFileName: selectedConfigName,
			ignoreSavedDefaultConfig: !selectedConfigName,
		});

		if (firstPass.status === 'prepared' || firstPass.status === 'needs_analysis_confirmation') {
			if (firstPass.change_impact && firstPass.change_impact.label) {
				const reason = firstPass.change_impact.reason ? ` (${firstPass.change_impact.reason})` : '';
				addMessage(`Change impact for ${fileName}: ${firstPass.change_impact.label}${reason}`, 'agent');
			}
			if (firstPass.change_detection && !isNewFileChangeImpact(firstPass.change_impact, firstPass.change_detection)) {
				const sheetSummary = summarizeSheetChanges(firstPass.change_detection.sheet_changes || {});
				const rowSummary = summarizeRowChanges(firstPass.change_detection);
				addMessage(`Change summary for ${fileName}:\n${sheetSummary}\n${rowSummary}`, 'agent');
			}
			addMessage(`Running AI analysis for ${fileName}...`, 'agent');
			const promptPreviewWidget = addPromptPreview(`Preparing prompt for ${fileName}...`, {
				title: 'Prompt Preview (Preparing)',
			});
			await flushChatUi();

			const confirmed = await runConfirmedPreparedAnalyze(fileName, selectedConfigName, promptPreviewWidget, firstPass.status === 'prepared' ? firstPass : null, true);
			if (confirmed.status === 'ok') {
				renderAnalyzeResult(confirmed);
				addMessage(`Finished analysis for ${fileName}. Moving to next file...`, 'agent');
			} else {
				showGuidedAnalyzeFailure(fileName, confirmed.message || confirmed.status, `${index + 2}/${fileNames.length}`);
			}
			await flushChatUi();
			await runPreparedAnalysisQueueAuto(fileNames, index + 1, selectedConfigName);
			return;
		}

		if (firstPass.status === 'ok') {
			renderAnalyzeResult(firstPass);
			addMessage(`Finished analysis for ${fileName}. Moving to next file...`, 'agent');
			await flushChatUi();
			await runPreparedAnalysisQueueAuto(fileNames, index + 1, selectedConfigName);
			return;
		}

		if (firstPass.status === 'needs_config_selection') {
			showConfigSelectionPrompt(
				firstPass.config_options || [],
				async (configName) => {
					addMessage(`Using selected config for auto queue: ${configName}`, 'agent');
					await runPreparedAnalysisQueueAuto(fileNames, index, configName);
				},
				() => {
					addMessage('Auto queue paused. Select a config and run again to continue.', 'agent');
				}
			);
			return;
		}

		if (firstPass.status === 'needs_config_upload') {
			addMessage('Auto queue paused: no config file found in data/config. Upload one to continue.', 'agent');
			pendingPreparedFilesAfterConfigUpload = fileNames.slice(index);
			promptChooseConfigFileForDir('guided_queue');
			return;
		}

		showGuidedAnalyzeFailure(fileName, firstPass.message || firstPass.status || 'Unknown error', `${index + 2}/${fileNames.length}`);
		await flushChatUi();
		await runPreparedAnalysisQueueAuto(fileNames, index + 1, selectedConfigName);
	}

	async function fetchPendingResponses() {
		try {
			const response = await fetch('http://localhost:5001/responses/pending');
			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				return { status: 'error', message: payload.message || 'Failed to list response files.' };
			}
			return payload;
		} catch (err) {
			return { status: 'error', message: err.message };
		}
	}

	async function archiveResponseRecord(recordFile) {
		try {
			const response = await fetch('http://localhost:5001/responses/archive', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ record_file: recordFile }),
			});
			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				return { status: 'error', message: payload.message || 'Failed to archive response file.' };
			}
			return payload;
		} catch (err) {
			return { status: 'error', message: err.message };
		}
	}

	async function executeAutoHandleResponse(recordFile) {
		try {
			const response = await fetch('http://localhost:5001/responses/auto-handle/execute', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ record_file: recordFile, mode: 'preview' }),
			});
			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				return { status: 'error', message: payload.message || 'Failed to execute auto-handle workflow.' };
			}
			return payload;
		} catch (err) {
			return { status: 'error', message: err.message };
		}
	}

	async function appendMappingsForRecord(recordFile) {
		try {
			const response = await fetch('http://localhost:5001/responses/append-mappings', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ record_file: recordFile }),
			});
			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				return { status: 'error', message: payload.message || 'Failed to append mappings.' };
			}
			return payload;
		} catch (err) {
			return { status: 'error', message: err.message };
		}
	}

	async function previewSmartUpdateForRecord(recordFile, fuzzyThreshold = 0.85) {
		try {
			const response = await fetch('http://localhost:5001/responses/smart-update/preview', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ record_file: recordFile, fuzzy_threshold: fuzzyThreshold }),
			});
			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				return { status: 'error', message: payload.message || 'Failed to build smart update preview.' };
			}
			return payload;
		} catch (err) {
			return { status: 'error', message: err.message };
		}
	}

	async function applySmartUpdateForRecord(recordFile, acceptedChanges, fuzzyThreshold = 0.85) {
		try {
			const response = await fetch('http://localhost:5001/responses/smart-update/apply', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					record_file: recordFile,
					accepted_changes: acceptedChanges,
					fuzzy_threshold: fuzzyThreshold,
				}),
			});
			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				return { status: 'error', message: payload.message || 'Failed to apply smart updates.' };
			}
			return payload;
		} catch (err) {
			return { status: 'error', message: err.message };
		}
	}

	function createSmartUpdateTable(rowData, titleText) {
		const wrapper = document.createElement('div');
		wrapper.classList.add('smart-update-table-wrap');

		const title = document.createElement('div');
		title.classList.add('smart-update-table-title');
		title.textContent = titleText;
		wrapper.appendChild(title);

		const table = document.createElement('table');
		table.classList.add('smart-update-table');

		const thead = document.createElement('thead');
		const headerRow = document.createElement('tr');
		const h1 = document.createElement('th');
		h1.textContent = 'Column';
		const h2 = document.createElement('th');
		h2.textContent = 'Value';
		headerRow.appendChild(h1);
		headerRow.appendChild(h2);
		thead.appendChild(headerRow);
		table.appendChild(thead);

		const tbody = document.createElement('tbody');
		Object.entries(rowData || {}).forEach(([key, value]) => {
			const tr = document.createElement('tr');
			const tdKey = document.createElement('td');
			tdKey.textContent = key;
			const tdValue = document.createElement('td');
			tdValue.textContent = value === null || value === undefined ? 'None' : String(value);
			tr.appendChild(tdKey);
			tr.appendChild(tdValue);
			tbody.appendChild(tr);
		});
		table.appendChild(tbody);
		wrapper.appendChild(table);
		return wrapper;
	}

	function promptSmartUpdateReview(previewPayload) {
		return new Promise((resolve) => {
			const preview = previewPayload && previewPayload.preview ? previewPayload.preview : {};
			const allProposals = Array.isArray(preview.proposals) ? preview.proposals : [];
			const proposals = allProposals.filter((proposal) => proposal.action !== 'update_exact');

			const msg = document.createElement('div');
			msg.classList.add('message', 'agent', 'smart-update-review');
			msg.style.display = 'flex';
			msg.style.flexDirection = 'column';
			msg.style.gap = '10px';

			const title = document.createElement('div');
			title.textContent = 'Smart Update Mappings Preview: select accepted changes, then apply. Exact matches are hidden from this checklist.';
			msg.appendChild(title);

			if (proposals.length === 0) {
				const noChanges = document.createElement('div');
				noChanges.textContent = 'No non-exact changes need review.';
				msg.appendChild(noChanges);
			}

			const controls = document.createElement('div');
			controls.classList.add('smart-update-controls');
			const selectAllLabel = document.createElement('label');
			selectAllLabel.classList.add('smart-update-select-all');
			const selectAll = document.createElement('input');
			selectAll.type = 'checkbox';
			selectAll.checked = true;
			selectAllLabel.appendChild(selectAll);
			selectAllLabel.appendChild(document.createTextNode(' Select All'));
			controls.appendChild(selectAllLabel);
			msg.appendChild(controls);

			const list = document.createElement('div');
			list.classList.add('smart-update-list');

			const selectable = [];
			proposals.forEach((proposal) => {
				const card = document.createElement('div');
				card.classList.add('smart-update-card');

				const header = document.createElement('div');
				header.classList.add('smart-update-card-header');

				const status = proposal.status || 'proposed';
				const isRejected = status === 'rejected' || proposal.action === 'rejected';
				const rowLabel = document.createElement('label');
				rowLabel.classList.add('smart-update-row-label');
				const cb = document.createElement('input');
				cb.type = 'checkbox';
				cb.checked = !isRejected;
				cb.disabled = isRejected;
				rowLabel.appendChild(cb);
				const labelText = document.createElement('span');
				const similarityText = SHOW_SMART_UPDATE_SIMILARITY && typeof proposal.similarity === 'number'
					? ` | similarity: ${proposal.similarity.toFixed(4)}`
					: '';
				labelText.textContent = `${proposal.change_id || '(unknown)'} | ${proposal.action || 'unknown'}${similarityText}`;
				rowLabel.appendChild(labelText);
				header.appendChild(rowLabel);

				if (proposal.target_sheet) {
					const location = document.createElement('div');
					location.classList.add('smart-update-location');
					const rowIndex = proposal.target_row_index ? ` row ${proposal.target_row_index}` : '';
					location.textContent = `Target: ${proposal.target_sheet}${rowIndex}`;
					header.appendChild(location);
				}

				card.appendChild(header);

				if (isRejected) {
					const err = document.createElement('div');
					err.classList.add('smart-update-error');
					err.textContent = Array.isArray(proposal.errors) && proposal.errors.length
						? proposal.errors.join(' | ')
						: 'Rejected by schema validation.';
					card.appendChild(err);
				} else if (proposal.action === 'append_new') {
					card.appendChild(createSmartUpdateTable(proposal.after_row || {}, 'New Row'));
					const sheets = Array.isArray(proposal.available_target_sheets) ? proposal.available_target_sheets : [];
					if (sheets.length > 1) {
						const selectorWrap = document.createElement('div');
						selectorWrap.classList.add('smart-update-sheet-select');
						const selectorLabel = document.createElement('label');
						selectorLabel.textContent = 'Worksheet for append:';
						const selector = document.createElement('select');
						sheets.forEach((sheetName) => {
							const opt = document.createElement('option');
							opt.value = sheetName;
							opt.textContent = sheetName;
							selector.appendChild(opt);
						});
						selectorWrap.appendChild(selectorLabel);
						selectorWrap.appendChild(selector);
						card.appendChild(selectorWrap);
						selectable.push({ changeId: proposal.change_id, checkbox: cb, sheetSelect: selector });
					} else {
						selectable.push({ changeId: proposal.change_id, checkbox: cb, sheetSelect: null });
					}
				} else {
					const compareWrap = document.createElement('div');
					compareWrap.classList.add('smart-update-compare');
					compareWrap.appendChild(createSmartUpdateTable(proposal.before_row || {}, 'Before'));
					compareWrap.appendChild(createSmartUpdateTable(proposal.after_row || {}, 'After'));
					card.appendChild(compareWrap);
					selectable.push({ changeId: proposal.change_id, checkbox: cb, sheetSelect: null });
				}

				list.appendChild(card);
			});

			selectAll.addEventListener('change', () => {
				selectable.forEach((entry) => {
					if (!entry.checkbox.disabled) {
						entry.checkbox.checked = selectAll.checked;
					}
				});
			});

			msg.appendChild(list);

			const actions = document.createElement('div');
			actions.classList.add('message-actions');

			const applyBtn = document.createElement('button');
			applyBtn.type = 'button';
			applyBtn.textContent = 'Apply Accepted';
			applyBtn.classList.add('chat-action-button', 'variant-yes');

			const cancelBtn = document.createElement('button');
			cancelBtn.type = 'button';
			cancelBtn.textContent = 'Cancel';
			cancelBtn.classList.add('chat-action-button', 'variant-cancel');

			applyBtn.onclick = () => {
				const accepted = selectable
					.filter((entry) => entry.checkbox.checked)
					.map((entry) => {
						if (entry.sheetSelect) {
							return { change_id: entry.changeId, target_sheet: entry.sheetSelect.value };
						}
						return { change_id: entry.changeId };
					});
				msg.remove();
				resolve(accepted);
			};

			cancelBtn.onclick = () => {
				msg.remove();
				resolve(null);
			};

			actions.appendChild(applyBtn);
			actions.appendChild(cancelBtn);
			msg.appendChild(actions);

			messages.appendChild(msg);
			scrollMessagesToBottom();
		});
	}

	function renderPromptQueueSummary(promptRecord, position, total) {
		const formatPromptTimestamp = (rawTimestamp) => {
			if (!rawTimestamp) return null;
			const parsed = new Date(rawTimestamp);
			if (Number.isNaN(parsed.getTime())) return rawTimestamp;
			return parsed.toLocaleString(undefined, {
				year: 'numeric',
				month: 'short',
				day: '2-digit',
				hour: '2-digit',
				minute: '2-digit',
				second: '2-digit',
				hour12: true,
				timeZoneName: 'short',
			});
		};

		const deriveFileName = () => {
			if (promptRecord.record_file && typeof promptRecord.record_file === 'string') {
				const parts = promptRecord.record_file.split('/');
				return parts[parts.length - 1] || '(unknown).json';
			}
			if (promptRecord.record_id && typeof promptRecord.record_id === 'string') {
				return `${promptRecord.record_id}.json`;
			}
			return '(unknown).json';
		};

		const lines = [];
		lines.push(`Prompt ${position}/${total}`);
		lines.push(`Filename: ${deriveFileName()}`);
		if (promptRecord.input_file_name) lines.push(`Input file: ${promptRecord.input_file_name}`);
		if (promptRecord.config_file_name) lines.push(`Config file: ${promptRecord.config_file_name}`);
		const formattedTimestamp = formatPromptTimestamp(promptRecord.created_at_utc);
		if (formattedTimestamp) lines.push(`Timestamp: ${formattedTimestamp}`);
		return lines.join('\n');
	}

	async function runResponseProcessingWorkflow() {
		addMessage('Starting AI response processing workflow...', 'agent');
		const pending = await fetchPendingResponses();
		if (!pending || pending.status === 'error') {
			addMessage('Response processing failed: ' + (pending?.message || 'Unknown error'), 'agent');
			return;
		}

		const responseRecords = Array.isArray(pending.responses) ? pending.responses : [];
		if (responseRecords.length === 0) {
			addMessage('No unprocessed LLM responses found in data/responses.', 'agent');
			return;
		}

		await processResponseQueue(responseRecords, 0);
	}

	async function processResponseQueue(responseRecords, index = 0) {
		if (index >= responseRecords.length) {
			addMessage('Finished AI response processing workflow.', 'agent');
			return;
		}

		const record = responseRecords[index];
		addMessage(renderPromptQueueSummary(record, index + 1, responseRecords.length), 'agent');
		if (record.response_preview) {
			addPromptPreview(record.response_preview, { title: 'LLM Response Preview' });
		} else if (record.prompt_preview) {
			addPromptPreview(record.prompt_preview, { title: 'Prompt Text Preview' });
		}

		addThreeOptionConfirmation('Choose how to handle this response file:', {
			yesLabel: 'Continue',
			skipLabel: 'Skip',
//			extraLabel: 'Automatically Handle',
//			extra2Label: 'Append Mappings',
			extra3Label: 'Update Config',
			cancelLabel: 'Cancel Remaining',
			yesClass: 'variant-yes',
			skipClass: 'variant-skip',
			cancelClass: 'variant-cancel',
//			extraClass: 'variant-auto',
//			extra2Class: 'variant-auto',
			extra3Class: 'variant-auto',
			onYes: async () => {
				const archived = await archiveResponseRecord(record.record_file);
				if (archived.status === 'error') {
					addMessage('Could not archive response: ' + archived.message, 'agent');
				} else {
					addMessage('Archived response file: ' + (archived.archived_file || record.record_file), 'agent');
				}
				await processResponseQueue(responseRecords, index + 1);
			},
			onSkip: async () => {
				addMessage('Skipped response (left in data/responses): ' + (record.record_id || record.record_file), 'agent');
				await processResponseQueue(responseRecords, index + 1);
			},
//			onExtra2: async () => {
//				const appendResult = await appendMappingsForRecord(record.record_file);
//				if (!appendResult || appendResult.status === 'error') {
//					addMessage('Append mappings failed: ' + (appendResult?.message || 'Unknown error'), 'agent');
//					await processResponseQueue(responseRecords, index + 1);
//					return;
//				}
//
//				const appendInfo = appendResult.append_result || {};
//				const rowsAppended = appendInfo.rows_appended ?? appendResult.rows_appended ?? 0;
//				addMessage(`Append Mappings complete: ${rowsAppended} row(s) appended to config file.`, 'agent');
//
//				addConfirmation(
//					'Archive this response file now?',
//					async () => {
//						const archived = await archiveResponseRecord(record.record_file);
//						if (archived.status === 'error') {
//							addMessage('Could not archive response: ' + archived.message, 'agent');
//						} else {
//							addMessage('Archived response file: ' + (archived.archived_file || record.record_file), 'agent');
//						}
//						await processResponseQueue(responseRecords, index + 1);
//					},
//					async () => {
//						addMessage('Response left in data/responses.', 'agent');
//						await processResponseQueue(responseRecords, index + 1);
//					}
//				);
//			},
			onExtra3: async () => {
				const previewResult = await previewSmartUpdateForRecord(record.record_file, 0.85);
				if (!previewResult || previewResult.status === 'error') {
					addMessage('Update config preview failed: ' + (previewResult?.message || 'Unknown error'), 'agent');
					await processResponseQueue(responseRecords, index + 1);
					return;
				}

				const preview = previewResult.preview || {};
				const summary = preview.summary || {};
				const allProposals = Array.isArray(preview.proposals) ? preview.proposals : [];
				const reviewableCount = allProposals.filter((proposal) => proposal.action !== 'update_exact').length;
				addMessage(
					`Update config preview: total ${summary.total || 0}, exact(hidden) ${summary.exact || 0}, reviewable ${reviewableCount}, append ${summary.append || 0}, rejected ${summary.rejected || 0}.`,
					'agent'
				);

				if (reviewableCount === 0) {
					addMessage('All detected mappings are exact matches. No manual review is required and no changes were applied.', 'agent');
					addConfirmation(
						'Archive this response file now?',
						async () => {
							const archived = await archiveResponseRecord(record.record_file);
							if (archived.status === 'error') {
								addMessage('Could not archive response: ' + archived.message, 'agent');
							} else {
								addMessage('Archived response file: ' + (archived.archived_file || record.record_file), 'agent');
							}
							await processResponseQueue(responseRecords, index + 1);
						},
						async () => {
							addMessage('Response left in data/responses.', 'agent');
							await processResponseQueue(responseRecords, index + 1);
						}
					);
					return;
				}

				const acceptedChanges = await promptSmartUpdateReview(previewResult);
				if (acceptedChanges === null) {
					addMessage('Update config cancelled. Response left in data/responses.', 'agent');
					await processResponseQueue(responseRecords, index + 1);
					return;
				}

				if (!Array.isArray(acceptedChanges) || acceptedChanges.length === 0) {
					addMessage('No changes were accepted. Nothing was applied.', 'agent');
					await processResponseQueue(responseRecords, index + 1);
					return;
				}

				const applyResult = await applySmartUpdateForRecord(record.record_file, acceptedChanges, 0.85);
				if (!applyResult || applyResult.status === 'error') {
					addMessage('Update config apply failed: ' + (applyResult?.message || 'Unknown error'), 'agent');
					await processResponseQueue(responseRecords, index + 1);
					return;
				}

				const stats = applyResult.apply_result || {};
				addMessage(
					`Update config complete: updated ${stats.updated_count || 0}, added ${stats.added_count || 0}, skipped ${stats.skipped_count || 0}, rejected ${stats.rejected_count || 0}. Exact matches were not shown in the review checklist.`,
					'agent'
				);

				addConfirmation(
					'Archive this response file now?',
					async () => {
						const archived = await archiveResponseRecord(record.record_file);
						if (archived.status === 'error') {
							addMessage('Could not archive response: ' + archived.message, 'agent');
						} else {
							addMessage('Archived response file: ' + (archived.archived_file || record.record_file), 'agent');
						}
						await processResponseQueue(responseRecords, index + 1);
					},
					async () => {
						addMessage('Response left in data/responses.', 'agent');
						await processResponseQueue(responseRecords, index + 1);
					}
				);
			},
			onExtra: async () => {
				const executeResult = await executeAutoHandleResponse(record.record_file);
				if (!executeResult || executeResult.status === 'error') {
					addMessage('Auto-handle execution failed: ' + (executeResult?.message || 'Unknown error'), 'agent');
					await processResponseQueue(responseRecords, index + 1);
					return;
				}

				const proposedLines = Array.isArray(executeResult.proposed_changes) ? executeResult.proposed_changes : [];
				const mappingPreview = Array.isArray(executeResult.mapping_line_preview) ? executeResult.mapping_line_preview : [];
				if (proposedLines.length > 0) {
					addMessage('Auto-handle execution output:\n' + proposedLines.map((line) => '- ' + line).join('\n'), 'agent');
				}
				if (mappingPreview.length > 0) {
					addMessage('Detected mapping lines:\n' + mappingPreview.map((line) => '- ' + line).join('\n'), 'agent');
				}
				if (executeResult.script_message) {
					addMessage(executeResult.script_message, 'agent');
				}

				const archiveAfterReview = async () => {
					addConfirmation(
						'Accept this auto-handle result and archive the response?',
						async () => {
							const archived = await archiveResponseRecord(record.record_file);
							if (archived.status === 'error') {
								addMessage('Could not archive response: ' + archived.message, 'agent');
							} else {
								addMessage('Archived response file: ' + (archived.archived_file || record.record_file), 'agent');
							}
							await processResponseQueue(responseRecords, index + 1);
						},
						async () => {
							addMessage('Auto-handle cancelled. Response left in data/responses.', 'agent');
							await processResponseQueue(responseRecords, index + 1);
						}
					);
				};

				if (executeResult.download_ready && executeResult.updated_config_csv) {
					addConfirmation(
						'Download the updated config CSV now?',
						async () => {
							downloadTextFile(
								executeResult.updated_config_csv,
								executeResult.updated_config_file_name || 'updated_config.csv',
								'text/csv;charset=utf-8;'
							);
							addMessage('Downloaded: ' + (executeResult.updated_config_file_name || 'updated_config.csv'), 'agent');
							await archiveAfterReview();
						},
						async () => {
							await archiveAfterReview();
						}
					);
					return;
				}

				await archiveAfterReview();
			},
			onCancel: () => {
				addMessage('Cancelled AI response processing workflow.', 'agent');
			},
		});
	}

	// Primary command: process changed files and prepare outputs for analysis.
	async function runFileProcessing() {
		addMessage('Processing files (change detection + preparation)...', 'agent');

		try {
			const response = await fetch('http://localhost:5001/run/file-processing', { method: 'POST' });
			const payload = await response.json();

			if (!response.ok || payload.status === 'error') {
				addMessage('⚠ ' + (payload.message || 'File processing failed.'), 'agent');
				if (payload?.stderr) {
					addMessage('⚠ Issues encountered:\n' + formatLog(payload.stderr), 'agent');
				}
				return;
			}

			if (payload.message) {
				addMessage(payload.message, 'agent');
			}

			if (window.debugMode) {
				if (payload.stdout) addMessage('[DEBUG] stdout:\n' + formatLog(payload.stdout), 'agent');
				if (payload.stderr) addMessage('[DEBUG] stderr:\n' + formatLog(payload.stderr), 'agent');
			} else if (payload.stderr) {
				addMessage('⚠ Issues encountered:\n' + formatLog(payload.stderr), 'agent');
			}

			const preparedFiles = payload?.file_details?.changed_files;
			if (Array.isArray(preparedFiles) && preparedFiles.length > 0) {
				const configStatus = await fetchAnalyzeConfigOptions();
			if (window.debugMode) {
				addMessage(`[DEBUG] Config status: ${JSON.stringify(configStatus)}`, 'agent');
			}

			if (configStatus && configStatus.status === 'none') {
				addMessage('No config file found in data/config directory.', 'agent');
				addMessage('Choose one from your files and it will be uploaded to data/config and saved there.', 'agent');
					pendingPreparedFilesAfterConfigUpload = preparedFiles;
					promptChooseConfigFileForDir('guided_queue');
					return;
				}

				if (configStatus && configStatus.status === 'single') {
					const selectedConfig = configStatus.selected_config;
					addMessage(`Config file found in data/config directory, using ${selectedConfig}.`, 'agent');
					addConfirmation(
						`Start guided AI analysis now for ${preparedFiles.length} prepared file(s)?`,
						async () => {
							await runPreparedAnalysisQueue(preparedFiles, 0, selectedConfig);
						},
						() => {
							addMessage('Skipped guided AI analysis. You can still run "analyze" later.', 'agent');
						}
					);
					return;
				}

				if (configStatus && configStatus.status === 'multiple') {
					//addMessage('Multiple config files found in data/config directory. Please choose one for this run.', 'agent');
					//addMessage('Recommendation: remove extras from data/config to avoid confusion.', 'agent');
					showConfigSelectionPrompt(
						configStatus.config_options || [],
						async (configName) => {
							addMessage(`Using selected config for guided queue: ${configName}`, 'agent');
							addConfirmation(
								`Start guided AI analysis now for ${preparedFiles.length} prepared file(s)?`,
								async () => {
									await runPreparedAnalysisQueue(preparedFiles, 0, configName);
								},
								() => {
									addMessage('Skipped guided AI analysis. You can still run "analyze" later.', 'agent');
								}
							);
						},
						async () => {
							addMessage('No config selected. Guided analysis was not started.', 'agent');
						}
					);
					return;
				}

				addConfirmation(
					`Start guided AI analysis now for ${preparedFiles.length} prepared file(s)?`,
					async () => {
						await runPreparedAnalysisQueue(preparedFiles, 0);
					},
					() => {
						addMessage('Skipped guided AI analysis. You can still run "analyze" later.', 'agent');
					}
				);
			}
		} catch (err) {
			addMessage('⚠ Server problem: ' + err.message, 'agent');
		}
	}


	function runCleanup() {
		callApi('/run/cleanup', 'Cleaning up files...', true);
	}

	function runOneDriveDownload() {
		callApi('/run/onedrive_download', 'Getting files from OneDrive...', true);
	}

	async function startOneDriveAuth() {
		addMessage('Starting OneDrive authentication...', 'agent');
		try {
			const response = await fetch('http://localhost:5001/run/onedrive_auth/start', {
				method: 'POST',
			});
			const payload = await response.json();

			if (!response.ok || payload.status === 'error') {
				addMessage('⚠ Could not start OneDrive authentication: ' + (payload.message || 'Unknown error'), 'agent');
				return;
			}

			if (payload.status === 'ok' && payload.already_authenticated) {
				addMessage('✓ OneDrive is already authenticated.', 'agent');
				return;
			}

			if (payload.status === 'pending') {
				oneDriveAuthSessionId = payload.auth_id || null;
				const authLines = [];
				authLines.push('Open this URL in your browser: ' + (payload.verification_uri || 'https://login.microsoft.com/device'));
				if (payload.user_code) {
					authLines.push('Enter code: ' + payload.user_code);
				}
				authLines.push('After sign-in, type: complete onedrive auth');
				addMessage(authLines.join('\n'), 'agent');
				return;
			}

			addMessage(payload.message || 'Authentication step started.', 'agent');
		} catch (err) {
			addMessage('⚠ Server problem: ' + err.message, 'agent');
		}
	}

	async function completeOneDriveAuth() {
		if (!oneDriveAuthSessionId) {
			addMessage('No pending OneDrive auth session found. Type: authenticate onedrive', 'agent');
			return;
		}

		addMessage('Checking OneDrive authentication status...', 'agent');
		try {
			const response = await fetch('http://localhost:5001/run/onedrive_auth/complete', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ auth_id: oneDriveAuthSessionId }),
			});
			const payload = await response.json();

			if (!response.ok || payload.status === 'error') {
				addMessage('⚠ OneDrive authentication failed: ' + (payload.message || 'Unknown error'), 'agent');
				oneDriveAuthSessionId = null;
				return;
			}

			if (payload.status === 'pending') {
				addMessage(payload.message || 'Authentication is still pending. Finish browser sign-in and try again.', 'agent');
				return;
			}

			if (payload.status === 'ok') {
				oneDriveAuthSessionId = null;
				addMessage(payload.message || '✓ OneDrive authentication successful.', 'agent');
				return;
			}

			addMessage(payload.message || 'Authentication check complete.', 'agent');
		} catch (err) {
			addMessage('⚠ Server problem: ' + err.message, 'agent');
		}
	}

	async function runOneDriveAuthSetup() {
		if (oneDriveAuthSessionId) {
			await completeOneDriveAuth();
			return;
		}
		await startOneDriveAuth();
	}

	// Primary command: run full sync pipeline.
	function runFileSync() {
		callApi('/run/file-sync', 'Running file sync (Google Drive -> OneDrive -> project)...', true);
	}

	function runGoogleToOneDrive() {
		callApi('/run/google_to_onedrive', 'Syncing files from Google Drive to OneDrive...', true);
	}

	function formatPromptPreview(promptText) {
		return String(promptText || '');
	}

	function addPromptPreview(promptText, options = {}) {
		const title = options.title || 'Prompt Preview';
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '6px';

		const heading = document.createElement('div');
		heading.textContent = title;
		heading.style.fontWeight = '600';
		msg.appendChild(heading);

		const box = document.createElement('pre');
		box.style.margin = '0';
		box.style.padding = '8px';
		box.style.background = '#f4f6f8';
		box.style.border = '1px solid #d0d7de';
		box.style.borderRadius = '6px';
		box.style.whiteSpace = 'pre-wrap';
		box.style.wordBreak = 'break-word';
		box.style.maxHeight = '240px';
		box.style.overflowY = 'auto';
		box.style.fontSize = '12px';
		box.textContent = formatPromptPreview(promptText);
		msg.appendChild(box);

		messages.appendChild(msg);
		scrollMessagesToBottom();

		return {
			setTitle(nextTitle) {
				heading.textContent = nextTitle;
			},
			setText(nextText) {
				box.textContent = formatPromptPreview(nextText);
				scrollMessagesToBottom();
			},
			remove() {
				msg.remove();
			},
		};
	}

	function updatePromptPreviewWidget(widget, promptText) {
		if (!widget) return;
		if (promptText) {
			widget.setTitle('Prompt Preview (Sent To Ollama Next)');
			widget.setText(promptText);
			return;
		}
		widget.setTitle('Prompt Preview');
		widget.setText('Prompt text not returned by API.');
	}

	// Calls the streaming /analyze/execute-stream endpoint and yields live output.
	// Returns the final payload when the stream completes.
	async function streamPreparedAnalyze(prepared, options = {}) {
		const { timeoutMs = 180000, onChunk = null } = options;
		let timeoutHandle;
		const controller = new AbortController();
		try {
			timeoutHandle = setTimeout(() => controller.abort(), timeoutMs);
			const response = await fetch('http://localhost:5001/analyze/execute-stream', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					prepared_prompt: prepared.prompt || '',
					prepared_prompt_file: prepared.prepared_prompt_file || '',
					prepared_input_file_info: prepared.input_file_info || '',
					prepared_config_file_info: prepared.config_file_info || '',
					prepared_config_file_name: prepared.prepared_config_file_name || prepared.selected_config || '',
					prepared_input_file_name: prepared.prepared_input_file_name || prepared.selected_input || '',
					prepared_input_column_data: prepared.input_column_data || null,
					prepared_input_row_data: prepared.input_row_data || null,
					prepared_config_column_data: prepared.config_column_data || null,
					prepared_change_status: prepared.prepared_change_status || prepared.change_detection?.status || '',
				}),
				signal: controller.signal,
			});

			if (!response.ok) {
				const errorText = await response.text();
				return { status: 'error', error: `API Error (${response.status}): ${errorText}` };
			}
			if (!response.body) {
				return { status: 'error', error: 'Streaming not supported in this browser.' };
			}

			const reader = response.body.getReader();
			const decoder = new TextDecoder();
			let buffer = '';

			while (true) {
				const { value, done } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });
				let newlineIndex = buffer.indexOf('\n');
				while (newlineIndex !== -1) {
					const rawLine = buffer.slice(0, newlineIndex).trim();
					buffer = buffer.slice(newlineIndex + 1);
					if (rawLine) {
						const event = JSON.parse(rawLine);
						if (event.event === 'chunk' && typeof onChunk === 'function') {
							onChunk(event.content || '');
						}
						if (event.event === 'complete') {
							return event.payload || { status: 'ok' };
						}
						if (event.event === 'error') {
							return event.payload || { status: 'error', error: 'Analysis failed.' };
						}
					}
					newlineIndex = buffer.indexOf('\n');
				}
			}
			return { status: 'error', error: 'Stream ended without a final result.' };
		} catch (err) {
			if (err?.name === 'AbortError') {
				return { status: 'error', error: `Analysis timed out after ${Math.floor(timeoutMs / 1000)} seconds.` };
			}
			return { status: 'error', error: `Stream failed: ${err.message}` };
		} finally {
			clearTimeout(timeoutHandle);
		}
	}

	// Shows the prepared prompt in an editable textarea with Send/Cancel buttons.
	// Returns a Promise that resolves with the (possibly edited) prompt text,
	// or null if the user cancels.
	function addEditablePromptPreview(promptText, options = {}) {
		const title = options.title || 'Review & Edit Prompt';
		return new Promise((resolve) => {
			const msg = document.createElement('div');
			msg.classList.add('message', 'agent');
			msg.style.display = 'flex';
			msg.style.flexDirection = 'column';
			msg.style.gap = '8px';

			const heading = document.createElement('div');
			heading.textContent = title;
			heading.style.fontWeight = '600';
			msg.appendChild(heading);

			const hint = document.createElement('div');
			hint.textContent = 'Edit the prompt below if needed, then click Send to Ollama.';
			hint.style.fontSize = '12px';
			hint.style.color = '#666';
			msg.appendChild(hint);

			const textarea = document.createElement('textarea');
			textarea.value = String(promptText || '');
			textarea.style.width = '100%';
			textarea.style.minHeight = '200px';
			textarea.style.maxHeight = '420px';
			textarea.style.padding = '8px';
			textarea.style.border = '1px solid #d0d7de';
			textarea.style.borderRadius = '6px';
			textarea.style.fontSize = '12px';
			textarea.style.fontFamily = 'monospace';
			textarea.style.resize = 'vertical';
			textarea.style.boxSizing = 'border-box';
			msg.appendChild(textarea);

			const btnRow = document.createElement('div');
			btnRow.style.display = 'flex';
			btnRow.style.gap = '8px';

			const sendBtn = document.createElement('button');
			sendBtn.type = 'button';
			sendBtn.textContent = 'Send to Ollama';
			sendBtn.classList.add('prompt-button');

			const cancelBtn = document.createElement('button');
			cancelBtn.type = 'button';
			cancelBtn.textContent = 'Cancel';
			cancelBtn.classList.add('prompt-button');
			cancelBtn.style.background = '#f4f6f8';
			cancelBtn.style.color = '#333';
			cancelBtn.style.border = '1px solid #d0d7de';

			sendBtn.onclick = () => { msg.remove(); resolve(textarea.value); };
			cancelBtn.onclick = () => { msg.remove(); resolve(null); };

			btnRow.appendChild(sendBtn);
			btnRow.appendChild(cancelBtn);
			msg.appendChild(btnRow);

			messages.appendChild(msg);
			scrollMessagesToBottom();
			textarea.focus();
		});
	}

	function _colWords(name) {
		return String(name || '')
			.replace(/([a-z])([A-Z])/g, '$1 $2')
			.toLowerCase()
			.split(/[_\s\-]+/)
			.filter((word) => word.length >= 2);
	}

	function _colSimilarity(a, b) {
		const an = String(a || '').toLowerCase().replace(/[_\s\-]/g, '');
		const bn = String(b || '').toLowerCase().replace(/[_\s\-]/g, '');
		if (an === bn) return 1;
		if (an.includes(bn) && an.length > 0) return bn.length / an.length;
		if (bn.includes(an) && bn.length > 0) return an.length / bn.length;

		const aw = _colWords(a);
		const bw = _colWords(b);
		if (!aw.length || !bw.length) return 0;
		const bSet = new Set(bw);
		const shared = aw.filter((word) => bSet.has(word)).length;
		return shared / Math.max(aw.length, bw.length);
	}

	function _buildColMapGreedy(configHeaders, inputHeaders) {
		const MIN_SCORE  = 0.35;
		const SOFT_SCORE = 0.15;

		// Build full score matrix once so both passes can reuse it.
		const scores = configHeaders.map((ch) =>
			inputHeaders.map((ih) => _colSimilarity(ch, ih))
		);

		// Pass 1: confident matches at MIN_SCORE threshold.
		const pass1Pairs = [];
		for (let configIndex = 0; configIndex < configHeaders.length; configIndex++) {
			for (let inputIndex = 0; inputIndex < inputHeaders.length; inputIndex++) {
				if (scores[configIndex][inputIndex] >= MIN_SCORE) {
					pass1Pairs.push({ configIndex, inputIndex, score: scores[configIndex][inputIndex] });
				}
			}
		}
		pass1Pairs.sort((left, right) => right.score - left.score);

		const colMap = new Array(configHeaders.length).fill(-1);
		const usedInput = new Set();
		for (const { configIndex, inputIndex } of pass1Pairs) {
			if (colMap[configIndex] === -1 && !usedInput.has(inputIndex)) {
				colMap[configIndex] = inputIndex;
				usedInput.add(inputIndex);
			}
		}

		// Pass 2: for any config slot still unfilled, try a softer threshold.
		// This prevents columns that belong in the template from being incorrectly
		// appended as new columns just because their name is slightly abbreviated.
		const pass2Pairs = [];
		for (let configIndex = 0; configIndex < configHeaders.length; configIndex++) {
			if (colMap[configIndex] !== -1) continue;
			for (let inputIndex = 0; inputIndex < inputHeaders.length; inputIndex++) {
				if (usedInput.has(inputIndex)) continue;
				if (scores[configIndex][inputIndex] >= SOFT_SCORE) {
					pass2Pairs.push({ configIndex, inputIndex, score: scores[configIndex][inputIndex] });
				}
			}
		}
		pass2Pairs.sort((left, right) => right.score - left.score);
		for (const { configIndex, inputIndex } of pass2Pairs) {
			if (colMap[configIndex] === -1 && !usedInput.has(inputIndex)) {
				colMap[configIndex] = inputIndex;
				usedInput.add(inputIndex);
			}
		}

		return colMap;
	}

	function _sheetBaseName(key) {
		const match = String(key || '').match(/\(([^)]+)\)/);
		return (match ? match[1] : String(key || '')).toLowerCase();
	}

	function _buildSheetMapGreedy(configSheetEntries, inputRowData) {
		const inputKeys = Object.keys(inputRowData || {});
		const pairs = [];
		for (let configIndex = 0; configIndex < configSheetEntries.length; configIndex++) {
			const [configSheetKey, configSheetData] = configSheetEntries[configIndex];
			const configHeaders = (configSheetData && configSheetData.column_names) || [];
			for (let inputIndex = 0; inputIndex < inputKeys.length; inputIndex++) {
				const inputKey = inputKeys[inputIndex];
				const inputSheetData = inputRowData[inputKey];
				const score = _sheetBaseName(configSheetKey) === _sheetBaseName(inputKey)
					? 1000
					: configHeaders.filter((configHeader) => ((inputSheetData && inputSheetData.headers) || []).some((inputHeader) => _colSimilarity(configHeader, inputHeader) >= 0.5)).length;
				pairs.push({ configIndex, inputIndex, score });
			}
		}
		pairs.sort((left, right) => right.score - left.score);

		const sheetMap = {};
		const usedInput = new Set();
		for (const { configIndex, inputIndex } of pairs) {
			if (!(configIndex in sheetMap) && !usedInput.has(inputIndex)) {
				sheetMap[configIndex] = inputRowData[inputKeys[inputIndex]];
				usedInput.add(inputIndex);
			}
		}
		return sheetMap;
	}

	function buildSyncedConfigCsv(inputColumnData, inputRowData, configColumnData) {
		const csvCell = (cell) => '"' + String(cell === null || cell === undefined ? '' : cell).replace(/"/g, '""') + '"';
		const csvRow = (row) => row.map(csvCell).join(',');

		if (
			configColumnData && typeof configColumnData === 'object' && Object.keys(configColumnData).length &&
			inputRowData && typeof inputRowData === 'object' && Object.keys(inputRowData).length
		) {
			const configSheetEntries = Object.entries(configColumnData);
			const sheetMap = _buildSheetMapGreedy(configSheetEntries, inputRowData);
			const allRows = [];
			for (let configIndex = 0; configIndex < configSheetEntries.length; configIndex++) {
				const [, configSheetData] = configSheetEntries[configIndex];
				const configHeaders = (configSheetData && configSheetData.column_names) || [];
				if (!configHeaders.length) continue;

				const matchedInputSheet = sheetMap[configIndex] || null;
				const inputHeaders = matchedInputSheet ? (matchedInputSheet.headers || []) : [];
				const inputRows = matchedInputSheet ? (matchedInputSheet.rows || []) : [];
				const colMap = _buildColMapGreedy(configHeaders, inputHeaders);
				const mappedInputIdxSet = new Set(colMap.filter((index) => index >= 0));
				const extraInputCols = inputHeaders
					.map((header, index) => ({ header, index }))
					.filter(({ index }) => !mappedInputIdxSet.has(index));

				const fullHeaders = [...configHeaders, ...extraInputCols.map(({ header }) => header)];
				if (allRows.length > 0) allRows.push([]);
				allRows.push(fullHeaders);

				for (const inputRow of inputRows) {
					const mappedValues = colMap.map((index) => (index >= 0 ? (inputRow[index] ?? '') : ''));
					const extraValues = extraInputCols.map(({ index }) => inputRow[index] ?? '');
					allRows.push([...mappedValues, ...extraValues]);
				}
			}
			if (allRows.length > 0) return allRows.map(csvRow).join('\n');
		}

		if (inputRowData && typeof inputRowData === 'object' && Object.keys(inputRowData).length) {
			const allRows = [];
			for (const [, sheetData] of Object.entries(inputRowData)) {
				const headers = (sheetData && sheetData.headers) || [];
				const dataRows = (sheetData && sheetData.rows) || [];
				if (!headers.length) continue;
				if (allRows.length > 0) allRows.push([]);
				allRows.push(headers);
				for (const row of dataRows) allRows.push(row);
			}
			if (allRows.length > 0) return allRows.map(csvRow).join('\n');
		}

		const rows = [['Sheet', 'Column Name', 'Column Position']];
		if (inputColumnData && typeof inputColumnData === 'object') {
			for (const [sheetKey, sheetData] of Object.entries(inputColumnData)) {
				const colNames = (sheetData && sheetData.column_names) || [];
				const colPositions = (sheetData && sheetData.column_positions) || [];
				for (let index = 0; index < colNames.length; index++) {
					const position = colPositions[index];
					const positionText = Array.isArray(position) ? position.join('') : (position || '');
					rows.push([sheetKey, colNames[index], positionText]);
				}
			}
		}
		return rows.map(csvRow).join('\n');
	}

	function downloadTextFile(content, filename, mimeType = 'text/plain;charset=utf-8;') {
		const blob = new Blob([content], { type: mimeType });
		const url = URL.createObjectURL(blob);
		const anchor = document.createElement('a');
		anchor.href = url;
		anchor.download = filename;
		document.body.appendChild(anchor);
		anchor.click();
		document.body.removeChild(anchor);
		URL.revokeObjectURL(url);
	}

	function downloadSyncedConfigCsv(inputColumnData, filename, inputRowData, configColumnData) {
		const csvContent = buildSyncedConfigCsv(inputColumnData, inputRowData, configColumnData);
		downloadTextFile(csvContent, filename, 'text/csv;charset=utf-8;');
	}

	function promptCsvDownload(inputColumnData, inputHint, inputRowData, configColumnData) {
		if (!inputColumnData || !Object.keys(inputColumnData).length) return;

		const safeName = String(inputHint || 'config')
			.replace(/\.[^.]+$/, '')
			.replace(/[^a-zA-Z0-9_\-]/g, '_');
		const filename = `${safeName}_synced_config.csv`;
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '8px';

		const text = document.createElement('div');
		text.textContent = `Download synced config CSV for ${safeName}?`;
		msg.appendChild(text);

		const buttonRow = document.createElement('div');
		buttonRow.style.display = 'flex';
		buttonRow.style.gap = '8px';

		const yesBtn = document.createElement('button');
		yesBtn.type = 'button';
		yesBtn.textContent = 'Download CSV';
		yesBtn.classList.add('prompt-button');
		yesBtn.onclick = () => {
			msg.remove();
			downloadSyncedConfigCsv(inputColumnData, filename, inputRowData, configColumnData);
			addMessage('Downloaded: ' + filename, 'agent');
		};

		const noBtn = document.createElement('button');
		noBtn.type = 'button';
		noBtn.textContent = 'No thanks';
		noBtn.classList.add('prompt-button');
		noBtn.style.background = '#f4f6f8';
		noBtn.style.color = '#333';
		noBtn.style.border = '1px solid #d0d7de';
		noBtn.onclick = () => msg.remove();

		buttonRow.appendChild(yesBtn);
		buttonRow.appendChild(noBtn);
		msg.appendChild(buttonRow);
		messages.appendChild(msg);
		scrollMessagesToBottom();
	}

	function renderAnalyzeResult(payload) {
		if (payload.input_file_info) {
			addMessage('Input: ' + payload.input_file_info, 'agent');
		}
		if (payload.config_file_info) {
			addMessage('Config: ' + payload.config_file_info, 'agent');
		}
		if (payload.selected_input || payload.selected_config) {
			const selected = [];
			if (payload.selected_input) selected.push('Input file: ' + payload.selected_input);
			if (payload.selected_config) selected.push('Config file: ' + payload.selected_config);
			addMessage(selected.join('\n'), 'agent');
		}
		if (payload.change_impact && payload.change_impact.label) {
			const reason = payload.change_impact.reason ? ` (${payload.change_impact.reason})` : '';
			addMessage('Change impact: ' + payload.change_impact.label + reason, 'agent');
		}
		if (payload.change_summary && !isNewFileChangeImpact(payload.change_impact, payload.change_detection)) {
			addMessage('Detected changes: ' + payload.change_summary, 'agent');
		}
		if (payload.prompt && !payload.prompt_preview_rendered) {
			addPromptPreview(payload.prompt);
		}
		if (payload.ai_summary && !payload.stream_rendered) {
			addMessage('AI Analysis Complete:', 'agent');
			addMessage(payload.ai_summary, 'agent');
		} else if (!payload.ai_summary && !payload.stream_rendered) {
			addMessage('No summary was generated. The model may have encountered an issue.', 'agent');
		}

		// if (payload.input_column_data && Object.keys(payload.input_column_data).length) {
		// 	const inputHint = payload.selected_input || payload.prepared_input_file_name || 'config';
		// 	promptCsvDownload(payload.input_column_data, inputHint, payload.input_row_data || null, payload.config_column_data || null);
		// }
	}

	function showAnalyzeConfirmationPrompt(payload, runConfirmedAnalyze) {
		const rawMessage = String(payload.message || '');
		const isDefaultConfirmMessage = rawMessage.includes('Confirm analyze by sending confirm_ai_analysis=true');
		if (rawMessage && !isDefaultConfirmMessage) {
			addMessage(rawMessage, 'agent');
		}

		if (payload.change_impact && payload.change_impact.label) {
			const reason = payload.change_impact.reason ? ` (${payload.change_impact.reason})` : '';
			if (payload.analysis_recommended === 'yes') {
				addMessage(
					'Change impact: ' +
					payload.change_impact.label +
					reason +
					' A previous version of this file has been analyzed before. Here are the changes compared to that. Recommendation: run AI analysis now.',
					'agent'
				);
			} else {
				addMessage(
					'Change impact: ' +
					payload.change_impact.label +
					reason +
					' No differences were detected versus the previously analyzed version. Recommendation: optional to run AI analysis.',
					'agent'
				);
			}
		}

		const changeDetection = payload.change_detection || {};
		const hasDetectedChanges = payload.analysis_recommended === 'yes';
		if (hasDetectedChanges) {
			const sheetSummary = summarizeSheetChanges(changeDetection.sheet_changes || {});
			const rowSummary = summarizeRowChanges(changeDetection);
			addMessage('Change summary:\n' + sheetSummary + '\n' + rowSummary, 'agent');
		}

		addConfirmation(
			'Run AI analysis with these files?',
			async () => {
				await runConfirmedAnalyze();
			},
			() => {
				addMessage('Skipped AI analysis for now. Baseline was not updated.', 'agent');
			}
		);
	}

	function showConfigSelectionPrompt(configOptions, onConfigChosen = null, onChooseDifferent = null) {
		const msg = document.createElement('div');
		msg.classList.add('message', 'agent');
		msg.style.display = 'flex';
		msg.style.flexDirection = 'column';
		msg.style.gap = '8px';

		const text = document.createElement('div');
		text.textContent = 'Multiple config files detected in data/config. Choose one to use (saved as default):';
		msg.appendChild(text);

		for (const configName of configOptions) {
			const btn = document.createElement('button');
			btn.type = 'button';
			btn.textContent = configName;
			btn.classList.add('prompt-button');
			btn.style.alignSelf = 'flex-start';
			btn.onclick = async () => {
				msg.remove();
				if (onConfigChosen) {
					await onConfigChosen(configName);
					return;
				}
				await analyzeUsingServerFiles(configName, true);
			};
			msg.appendChild(btn);
		}

		const uploadBtn = document.createElement('button');
		uploadBtn.type = 'button';
		uploadBtn.textContent = 'Use a different config file';
		uploadBtn.classList.add('prompt-button');
		uploadBtn.style.alignSelf = 'flex-start';
		uploadBtn.onclick = () => {
			msg.remove();
			if (onChooseDifferent) {
				onChooseDifferent();
			}
			analyzeConfigUploadPending = true;
			fileInput.accept = '.csv,.xlsx,.xls,.xlsm';
			fileInput.value = '';
			fileInput.click();
			addMessage('Choose a config file from your files. It will be uploaded to data/config and saved there.', 'agent');
		};
		msg.appendChild(uploadBtn);

		messages.appendChild(msg);
		scrollMessagesToBottom();
	}

	async function analyzeUsingServerFiles(configFileName = null, saveDefaultConfig = false, confirmAiAnalysis = false) {
		let elapsedTicker = null;
		try {
			if (configFileName) {
				addMessage('Using selected config: ' + configFileName, 'agent');
			}
			addMessage('Checking input/config files...', 'agent');
			let payload;

			if (confirmAiAnalysis) {
				const prepareStatus = createLiveStatusMessage('Preparing prompt from auto-detected input/config files... elapsed time: 0 seconds');
				const prepareStartedAt = Date.now();
				elapsedTicker = setInterval(() => {
					const elapsedSeconds = Math.floor((Date.now() - prepareStartedAt) / 1000);
					prepareStatus(`Preparing prompt from auto-detected input/config files... elapsed time: ${elapsedSeconds} seconds`);
				}, 1000);

				const prepared = await analyzePreparedInputFile(null, {
					confirmAiAnalysis: true,
					configFileName,
					saveDefaultConfig,
					prepareOnly: true,
				});
				if (elapsedTicker) {
					clearInterval(elapsedTicker);
					elapsedTicker = null;
				}

				if (!prepared || prepared.status === 'error') {
					addMessage('Could not auto-detect files for analyze. Switching to manual file selection.', 'agent');
					return false;
				}

				if (prepared.status !== 'prepared') {
					payload = prepared;
				} else {
					// Auto-run: automatically accept the prompt without showing edit dialog
					const autoPrompt = prepared.prompt || '';

					await showLlmStartupDiagnostics();
					const runStartedAt = Date.now();
					const runStatus = createLiveStatusMessage('Ollama is processing... elapsed time: 0 seconds');
					elapsedTicker = setInterval(() => {
						const elapsedSeconds = Math.floor((Date.now() - runStartedAt) / 1000);
						runStatus(`Ollama is processing... elapsed time: ${elapsedSeconds} seconds`);
					}, 1000);

					addMessage('AI Analysis Streaming:', 'agent');
					const liveSummaryAuto = addLiveMessage('', 'agent');
					payload = await streamPreparedAnalyze(
						{ ...prepared, prompt: autoPrompt },
						{
							timeoutMs: 180000,
							onChunk: (chunk) => { liveSummaryAuto.appendText(chunk); },
						},
					);
					if (elapsedTicker) { clearInterval(elapsedTicker); elapsedTicker = null; }
					if (payload) {
						payload.prompt_preview_rendered = true;
						payload.stream_rendered = true;
						if (!payload.prompt) payload.prompt = autoPrompt;
					}
				}
			} else {
				addMessage('Analyzing with auto-detected files... this can take up to a few minutes.', 'agent');
				payload = await analyzePreparedInputFile(null, {
					confirmAiAnalysis,
					configFileName,
					saveDefaultConfig,
				});
			}

			if (!payload || payload.status === 'error') {
				addMessage('Could not auto-detect files for analyze. Switching to manual file selection.', 'agent');
				return false;
			}

			if (payload.status === 'ok') {
				renderAnalyzeResult(payload);
				return true;
			}

			if (payload.status === 'needs_config_selection') {
				showConfigSelectionPrompt(payload.config_options || []);
				return true;
			}

			if (payload.status === 'needs_config_upload') {
				addMessage(payload.message || 'No config file detected in data/config.', 'agent');
				promptChooseConfigFileForDir('analyze');
				return true;
			}

			if (payload.status === 'needs_analysis_confirmation') {
				showAnalyzeConfirmationPrompt(payload, async () => {
					await analyzeUsingServerFiles(configFileName, saveDefaultConfig, true);
				});
				return true;
			}

			if (payload.status === 'needs_input_upload') {
				addMessage(payload.message || 'No input file detected. Upload input + config manually.', 'agent');
				return false;
			}

			if (payload.error) {
				addMessage('Analysis Error:', 'agent');
				addMessage(payload.error, 'agent');
				return true;
			}

			return false;
		} catch (err) {
			addMessage('Auto-detect analyze failed: ' + err.message, 'agent');
			return false;
		} finally {
			if (elapsedTicker) clearInterval(elapsedTicker);
		}
	}

	async function checkFileChanges() {
		if (!window.lastUploadedFile || !window.lastUploadedFile.name) {
			addMessage('No uploaded file found. Use input first.', 'agent');
			return;
		}

		addMessage('Checking file changes...', 'agent');
		try {
			const response = await fetch('http://localhost:5001/check_file_changes', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ file_name: window.lastUploadedFile.name, update_baseline: false }),
			});

			const payload = await response.json();
			if (!response.ok || payload.status === 'error') {
				addMessage('⚠ ' + (payload.message || 'Failed to check file changes.'), 'agent');
				return;
			}

			if (payload.status === 'first_version') {
				addMessage('This is the first saved version. Upload another version to compare changes.', 'agent');
				return;
			}

			if (payload.status === 'no_change') {
				addMessage('No changes detected from the saved baseline.', 'agent');
				return;
			}

			if (payload.status === 'changed') {
				addMessage('Changes detected from the saved baseline.', 'agent');
				const sheetSummary = summarizeSheetChanges(payload.sheet_changes || {});
				const rowSummary = summarizeRowChanges(payload);
				addMessage('Change summary:\n' + sheetSummary + '\n' + rowSummary, 'agent');
				return;
			}

			addMessage('Change check complete: ' + JSON.stringify(payload), 'agent');
		} catch (err) {
			addMessage('⚠ Server problem: ' + err.message, 'agent');
		}
	}

	async function extractColumnsFromSelectedFile(file) {
		if (!file) {
			addMessage('No file selected.', 'agent');
			return;
		}

		const lowerName = file.name.toLowerCase();
		if (!(lowerName.endsWith('.xlsx') || lowerName.endsWith('.xls') || lowerName.endsWith('.xlsm'))) {
			addMessage('Please choose an Excel file (.xlsx, .xls, .xlsm).', 'agent');
			return;
		}
		try {
			const formData = new FormData();
			formData.append('file', file);
			const uploadResponse = await fetch('http://localhost:5001/upload_and_process', {
				method: 'POST',
				body: formData,
			});
			const uploadPayload = await uploadResponse.json();

			if (!uploadResponse.ok || uploadPayload.status === 'error') {
				addMessage('⚠ ' + (uploadPayload.message || 'Upload failed.'), 'agent');
				return;
			}

			const extractResponse = await fetch(
				'http://localhost:5001/run/extract-columns?file_name=' + encodeURIComponent(file.name),
				{ method: 'POST' }
			);
			const extractPayload = await extractResponse.json();

			if (!extractResponse.ok || extractPayload.status === 'error') {
				addMessage('⚠ ' + (extractPayload.message || 'Failed to extract columns.'), 'agent');
				return;
			}

			if (extractPayload.message) {
				addMessage(extractPayload.message, 'agent');
			}

			const columnData = extractPayload.column_data || {};
			const lines = [];

			//this is new for sheet iteratrion and column display in the chat output
			Object.entries(columnData).forEach(([sheetKey, sheetData]) => {
				if (!sheetData || typeof sheetData !== 'object') {
					return;
				}
				const names = Array.isArray(sheetData.column_names) ? sheetData.column_names : [];
				const positions = Array.isArray(sheetData.column_positions) ? sheetData.column_positions : [];
				if (!names.length) {
					return;
				}

				lines.push(`${sheetKey}:`);
				names.forEach((name, index) => {
					const pos = positions[index];
					const at = Array.isArray(pos) && pos.length === 2 ? ` (${pos[0]}${pos[1]})` : '';
					lines.push(`${index + 1}. ${name}${at}`);
				});
			});

			if (!lines.length) {
				addMessage('No non-empty columns were found.', 'agent');
				return;
			}

			addMessage('Column Extract Output:\n' + lines.join('\n'), 'agent');
		} catch (err) {
			addMessage('⚠ Server problem: ' + err.message, 'agent');
		}
	}

	function extractColumnsFromExcel() {
		if (!fileInput) {
			addMessage('File input element not found.', 'agent');
			return;
		}

		extractColumnsPending = true;
		fileInput.accept = '.xlsx,.xls,.xlsm';
		fileInput.value = '';
		fileInput.click();
		addMessage('Choose an Excel file for column extraction.', 'agent');
	}

	// Analyzes two files (input + config) using Ollama LLM via API endpoint /analyze-excel_files.
	// Sends both files and displays the AI summary.
	async function analyzeFiles(inputFile, configFile, options = {}) {
		let timeoutHandle = null;
		let elapsedTicker = null;
		let prepareTicker = null;
		let updateStatusLine = null;
		let promptPreviewWidget = null;
		let timeoutMs = 180000;
		try {
			const confirmAiAnalysis = options.confirmAiAnalysis === true;
			const retryEnabled = options.enableBackoffRetries === true;
			const maxRetries = Number.isInteger(options.maxRetries) ? options.maxRetries : 3;
			const backoffInitialSeconds = Number(options.backoffInitialSeconds || 3);
			const backoffMultiplier = Number(options.backoffMultiplier || 2);
			const backoffMaxSeconds = Number(options.backoffMaxSeconds || 30);

			// Phase 1: Prepare change-aware prompt data (including diff context) before generation starts.

			const prepareData = new FormData();
			prepareData.append('input_file', inputFile);
			prepareData.append('config_file', configFile);
			prepareData.append('prepare_only', 'true');
			prepareData.append('confirm_ai_analysis', confirmAiAnalysis ? 'true' : 'false');
			prepareData.append('allow_retry', retryEnabled ? 'true' : 'false');
			prepareData.append('max_retries', String(retryEnabled ? maxRetries : 0));
			prepareData.append('backoff_initial_seconds', String(backoffInitialSeconds));
			prepareData.append('backoff_multiplier', String(backoffMultiplier));
			prepareData.append('backoff_max_seconds', String(backoffMaxSeconds));

			const prepareStatusLine = createLiveStatusMessage('Preparing prompt from selected files... elapsed time: 0 seconds');
			const prepareStartedAt = Date.now();
			prepareTicker = setInterval(() => {
				const elapsedSeconds = Math.floor((Date.now() - prepareStartedAt) / 1000);
				prepareStatusLine(`Preparing prompt from selected files... elapsed time: ${elapsedSeconds} seconds`);
			}, 1000);

			const prepareResponse = await fetch('http://localhost:5001/analyze-excel_files', {
				method: 'POST',
				body: prepareData,
			});
			if (prepareTicker) {
				clearInterval(prepareTicker);
				prepareTicker = null;
			}

			if (!prepareResponse.ok) {
				const errorText = await prepareResponse.text();
				addMessage(`API Error (${prepareResponse.status}): ${errorText}`, 'agent');
				return;
			}

			const prepared = await prepareResponse.json();
			if (prepared.error) {
				addMessage('Analysis Error:', 'agent');
				addMessage(prepared.error, 'agent');
				return;
			}

			if (prepared.status === 'needs_analysis_confirmation') {
				showAnalyzeConfirmationPrompt(prepared, async () => {
					await analyzeFiles(inputFile, configFile, {
						...options,
						confirmAiAnalysis: true,
					});
				});
				return;
			}

			addMessage('Data extraction and file-change context are ready. Review the prompt below before sending.', 'agent');

			// Phase 2: Let the client review or edit before sending to Ollama.
			const editedPromptFiles = await addEditablePromptPreview(prepared.prompt || '', {
				title: 'Review & Edit Prompt (uploaded files)',
			});
			if (editedPromptFiles === null) {
				addMessage('Analysis cancelled.', 'agent');
				return;
			}

			timeoutMs = retryEnabled ? 360000 : 180000;
			await showLlmStartupDiagnostics();

			const startedAt = Date.now();
			updateStatusLine = createLiveStatusMessage('Ollama is processing... elapsed time: 0 seconds');
			elapsedTicker = setInterval(() => {
				const elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
				updateStatusLine(`Ollama is processing... elapsed time: ${elapsedSeconds} seconds`);
			}, 1000);

			addMessage('AI Analysis Streaming:', 'agent');
			const liveSummaryFiles = addLiveMessage('', 'agent');
			const payload = await streamPreparedAnalyze(
				{ ...prepared, prompt: editedPromptFiles },
				{
					timeoutMs,
					onChunk: (chunk) => { liveSummaryFiles.appendText(chunk); },
				},
			);

			// Show file details from the streamed payload.
			if (payload.input_file_info) {
				addMessage('Input: ' + payload.input_file_info, 'agent');
			}
			if (payload.config_file_info) {
				addMessage('Config: ' + payload.config_file_info, 'agent');
			}

			// Handle errors from the stream.
			if (payload.error || payload.status === 'error') {
				const errMsg = payload.error || payload.message || 'Analysis failed.';
				addMessage('Analysis Error: ' + errMsg, 'agent');
				if (payload.attempts) {
					addMessage(`Attempts used: ${payload.attempts}`, 'agent');
				}
				const isRetryable = payload.retryable === true;
				if (isRetryable && !retryEnabled) {
					addConfirmation(
						'Do you want to retry automatically with exponential backoff (3 retries at 3s, 6s, 12s)?',
						() => analyzeFiles(inputFile, configFile, {
							enableBackoffRetries: true,
							maxRetries: 3,
							backoffInitialSeconds: 3,
							backoffMultiplier: 2,
							backoffMaxSeconds: 30,
						}),
						() => addMessage('Retry skipped. You can type "analyze" to run again manually.', 'agent')
					);
				}
				if (errMsg.includes('timed out')) {
					addMessage('Suggestion: retry once; if it keeps timing out, use a smaller model or raise OLLAMA_READ_TIMEOUT_SECONDS.', 'agent');
				} else if (errMsg.includes('Cannot connect')) {
					addMessage('Suggestion: make sure Ollama is running locally (`ollama serve`) and reachable at http://localhost:11434.', 'agent');
				}
				return;
			}

			// ai_summary was already shown live via streaming; only show if stream was empty.
			if (payload.ai_summary && !liveSummaryFiles.element.textContent) {
				addMessage('AI Analysis Complete:', 'agent');
				addMessage(payload.ai_summary, 'agent');
			} else if (!payload.ai_summary && !liveSummaryFiles.element.textContent) {
				addMessage('No summary was generated. The model may have encountered an issue.', 'agent');
			}

			// if (prepared.input_column_data && Object.keys(prepared.input_column_data).length) {
			// 	const inputHint = inputFile.name || prepared.prepared_input_file_name || 'config';
			// 	promptCsvDownload(prepared.input_column_data, inputHint, prepared.input_row_data || null, prepared.config_column_data || null);
			// }

		} catch (err) {
			// streamPreparedAnalyze handles AbortError/timeouts internally and returns a result object,
			// so this catch only fires for truly unexpected errors (network failure before stream starts, etc.).
			addMessage('Failed to analyze files:', 'agent');
			addMessage(err.message, 'agent');
			addMessage('Check browser console (F12) and API logs for more details.', 'agent');
		} finally {
			if (timeoutHandle) clearTimeout(timeoutHandle);
			if (elapsedTicker) clearInterval(elapsedTicker);
			if (prepareTicker) clearInterval(prepareTicker);
		}
	}

	// Starts explicit two-file analyze flow (user picks input, then config).
	function startAnalyze() {
		if (!fileInput) {
			addMessage('File input element not found.', 'agent');
			return;
		}
		analyzeState = { inputFile: null, configFile: null };
		addMessage('Starting AI analysis. First, choose the INPUT file...', 'agent');
		fileInput.accept = '.csv,.xlsx,.xls,.xlsm';
		fileInput.value = '';
		fileInput.click();
	}

	// Removed commands: They still exist in the backend but aren't useful anymore, and add too much clutter.
	// They can be re-enabled if needed for testing or future features.
	
// === DEPRECATED: PLAN TO REMOVE SOON ===
// 11. run excel scripts - Alias for 'run file processing'
// 12. run validate - Validation-only step
// 13. run transform - Transformation-only step

	// Shows all available commands.
	function showHelp() {
		const helpText = `Available commands (type the command or its number):

=== MAIN WORKFLOWS ===
1. run file processing - Detect changes in input folder & prepare files for LLM analysis.
2. run file sync - Full sync: Google Drive -> OneDrive -> project input. Requires authentication tokens for Microsoft API (commands 5 and 6).
3. process responses - Process pending LLM response JSON files in data/responses, extract modification suggestions, and apply to specified config files in data/config.
4. input - Upload a file into project input (or place files manually in data/input).

=== TOOL SETUP ===
5. authenticate onedrive - Start/continue browser-based OneDrive auth.
6. complete onedrive auth - Explicitly complete auth after browser sign-in. Saves a token to data/state, which is used for the onedrive sync.

=== INDIVIDUAL COMMANDS FOR TESTING ===
7. analyze - AI analysis: Upload file + config, analyze with Ollama
8. download from onedrive - OneDrive download to project input (requires auth)
9. run google to onedrive - Sync files Google Drive -> OneDrive
10. cleanup - Reset all generated state (hashes, outputs, archive, etc.) except files in input folder and Authentication tokens for OneDrive and Google Drive. For testing purposes.

Type 'help' or '0' for this menu`;
		addMessage(helpText, 'agent');
	}

	function toggleDebugMode() {
		window.debugMode = !window.debugMode;
		localStorage.setItem('agentic-debug-mode', window.debugMode ? 'true' : 'false');
		const status = window.debugMode ? 'enabled' : 'disabled';
		addMessage(`Debug mode ${status}`, 'agent');
	}


	// When user presses send or hits enter
	function sendUserMessage() {
		const text = input.value.trim();
		if (!text) return;

		addMessage(text, 'user');
		input.value = '';
		input.focus();

		const normalized = text.toLowerCase();

		// Main workflows (numbered 1-2)
		if (
			normalized === 'run file processing' ||
			normalized === 'file processing' ||
			normalized === 'process files' ||
			normalized === '1'
		) {
			runFileProcessing();
			return;
		}
		if (
			normalized === 'run file sync' ||
			normalized === 'file sync' ||
			normalized === 'sync files' ||
			normalized === '2'
		) {
			runFileSync();
			return;
		}

		// Command groups with renumbered shortcuts
		if (
			normalized === 'process responses' ||
			normalized === 'response processing' ||
			normalized === '3'
		) {
			runResponseProcessingWorkflow();
			return;
		}
		if (normalized === 'input' || normalized === '4') {
			triggerFileInput();
			return;
		}
		if (
			normalized === 'authenticate onedrive' ||
			normalized === 'setup onedrive auth' ||
			normalized === 'onedrive auth setup' ||
			normalized === 'onedrive auth' ||
			normalized === 'auth onedrive' ||
			normalized === '5'
		) {
			runOneDriveAuthSetup();
			return;
		}
		if (
			normalized === 'complete onedrive auth' ||
			normalized === 'finish onedrive auth' ||
			normalized === 'onedrive auth complete' ||
			normalized === '6'
		) {
			completeOneDriveAuth();
			return;
		}
		if (normalized === 'analyze' || normalized === 'analyze files' || normalized === '7') {
			startAnalyze();
			return;
		}
		if (
			normalized === 'download from onedrive' ||
			normalized === 'sync from onedrive' ||
			normalized === 'onedrive download' ||
			normalized === 'download onedrive' ||
			normalized === '8'
		) {
			runOneDriveDownload();
			return;
		}
		if (
			normalized === 'run google to onedrive' ||
			normalized === 'google to onedrive' ||
			normalized === 'sync google to onedrive' ||
			normalized === 'run google sync' ||
			normalized === '9'
		) {
			runGoogleToOneDrive();
			return;
		}
		if (
			normalized === 'cleanup' ||
			normalized === 'clean' ||
			normalized === 'delete output and hashes' ||
			normalized === 'clear output and hashes' ||
			normalized === 'delete output/hash files' ||
			normalized === 'reset onedrive manifest' ||
			normalized === 'reset manifest' ||
			normalized === '10'
		) {
			runCleanup();
			return;
		}
		if (normalized === 'help' || normalized === '?' || normalized === '0') {
			showHelp();
			return;
		}

		setTimeout(() => {
			addMessage('You said: ' + text, 'agent');
		}, 600);
	}

	sendBtn.addEventListener('click', sendUserMessage);

	input.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') {
			e.preventDefault();
			sendUserMessage();
		}
	});

	function initializeChatStartupView() {
		const focusInputWithoutScroll = () => {
			if (!input) return;
			try {
				input.focus({ preventScroll: true });
			} catch {
				input.focus();
			}
		};

		requestAnimationFrame(() => {
			window.scrollTo(0, 0);
			if (messages) {
				messages.scrollTop = 0;
			}
			focusInputWithoutScroll();

			requestAnimationFrame(() => {
				window.scrollTo(0, 0);
				if (messages) {
					messages.scrollTop = 0;
				}
				focusInputWithoutScroll();
			});
		});
	}

	initializeChatStartupView();

	// Removed global auto-scroll observer to prevent aggressive snap-to-bottom behavior.

});


async function _debug_refreshFileList() {
    try {
        const response = await fetch('http://localhost:5001/debug/files');

        if (!response.ok) {
            throw new Error(`HTTP error: ${response.status}`);
        }

        const data = await response.json();

        const inputList = document.getElementById('input-files');
        const configList = document.getElementById('config-files');
        const inputHeader = document.getElementById('input-header');
        const configHeader = document.getElementById('config-header');
        const debugPanel = document.getElementById('folder-debug');

        if (!inputList || !configList || !inputHeader || !configHeader || !debugPanel) return;

        // Show the panel
        debugPanel.style.display = 'block';

        // Update headers with counts
        inputHeader.textContent = `data/input (${data.input_files.length} file${data.input_files.length !== 1 ? 's' : ''})`;
        configHeader.textContent = `data/config (${data.config_files.length} file${data.config_files.length !== 1 ? 's' : ''})`;

        // Populate lists
        inputList.innerHTML = data.input_files.length
            ? data.input_files.map(f => `<li>📄 ${f}</li>`).join('')
            : '<li style="color: #999; font-style: italic;">(empty)</li>';

        configList.innerHTML = data.config_files.length
            ? data.config_files.map(f => `<li>⚙️ ${f}</li>`).join('')
            : '<li style="color: #999; font-style: italic;">(empty)</li>';

    } catch (err) {
        console.error('Debug file list error:', err);
    }
}

// Auto-run on page load
document.addEventListener('DOMContentLoaded', function () {
    const btn = document.getElementById('refresh-files');
    if (btn) btn.addEventListener('click', _debug_refreshFileList);
    _debug_refreshFileList();
});


// Auto-run safely
document.addEventListener('DOMContentLoaded', function () {
    const btn = document.getElementById('refresh-files');
    if (btn) btn.addEventListener('click', _debug_refreshFileList);

    _debug_refreshFileList();
});