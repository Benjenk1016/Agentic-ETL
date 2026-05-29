# Frontend API & Function Documentation

This document provides a **complete reference** for the frontend logic in `chat_intake.js`, including:

- API endpoints and how they are used
- Function responsibilities and relationships
- Execution flow of the application
- Identification of unused / legacy code

This is intended for future developers maintaining or extending the tool.

---

# API Endpoints

## Change Detection & File Upload

- **`POST /check_file_changes`**
  - **Used in:** `runAutoChangeDetectionForUpload`, `checkFileChanges`
  - **Sends:** `{ file_name, update_baseline }`
  - **Receives:** Change detection results
  - **Purpose:**  
    - Compares uploaded file against baseline  
    - Determines structural/data changes  

---

- **`POST /upload_and_process`**
  - **Used in:** `uploadAndProcess`, `extractColumnsFromSelectedFile`
  - **Sends:** multipart file data
  - **Receives:** Upload status + change results
  - **Purpose:**  
    - Upload file to backend  
    - Trigger validation + change detection  

---

## Analysis (LLM / AI)

- **`POST /analyze-excel_files-from-input`**
  - **Used in:** `analyzePreparedInputFile`
  - **Sends:** prepared input/config data
  - **Receives:** analysis payload or results
  - **Purpose:**  
    - Core analysis endpoint  
    - Supports:
      - prepare-only mode
      - full execution
      - cached payload reuse
      - timeout handling  

---

- **`POST /analyze-excel_files`**
  - **Used in:** `analyzeFiles`
  - **Sends:** input + config files
  - **Purpose:**  
    - Runs direct two-file analysis  

---

- **`POST /analyze/execute-stream`**
  - **Used in:** `streamPreparedAnalyze`
  - **Sends:** prepared prompt
  - **Receives:** streaming output
  - **Purpose:**  
    - Streams LLM responses to UI  

---

- **`GET /analyze/config-options`**
  - **Used in:** `fetchAnalyzeConfigOptions`, `runFileProcessing`
  - **Purpose:**  
    - Retrieves available config files  

---

## Config Management

- **`POST /upload_config`**
  - **Used in:** `uploadConfigToConfigDir`
  - **Purpose:**  
    - Upload config file to backend directory  

---

## LLM Diagnostics

- **`GET /llm/startup-diagnostics`**
  - **Used in:** `showLlmStartupDiagnostics`
  - **Purpose:**  
    - Check model readiness  

---

## Response Processing

- **`GET /responses/pending`**
  - **Used in:** `fetchPendingResponses`
  - **Purpose:** List unprocessed responses  

- **`POST /responses/archive`**
  - **Used in:** `archiveResponseRecord`
  - **Sends:** `{ record_file }`
  - **Purpose:** Archive processed response  

- **`POST /responses/auto-handle/execute`**
  - **Used in:** `executeAutoHandleResponse`
  - **Purpose:** Run automated handling  

- **`POST /responses/append-mappings`**
  - **Used in:** `appendMappingsForRecord`
  - **Purpose:** Append config mappings  

- **`POST /responses/smart-update/preview`**
  - **Used in:** `previewSmartUpdateForRecord`
  - **Purpose:** Generate update preview  

- **`POST /responses/smart-update/apply`**
  - **Used in:** `applySmartUpdateForRecord`
  - **Purpose:** Apply selected updates  

---

## File Processing Pipeline

  - **Used in:** `runFileProcessing`
  - **Purpose:** Run full pipeline  

  - **Used in:** `runValidateOnly`

  - **Used in:** `runTransformOnly`

  - **Used in:** `runCleanup`

---

- **`POST /run/onedrive_auth/start`**
  - **Used in:** `startOneDriveAuth`

- **`POST /run/onedrive_auth/complete`**
  - **Used in:** `completeOneDriveAuth`

- **`POST /run/file-sync`**
  - **Used in:** `runFileSync`

- **`POST /run/google_to_onedrive`**
  - **Used in:** `runGoogleToOneDrive`

---

## File Utilities

- **`POST /run/extract-columns`**
  - **Used in:** `extractColumnsFromSelectedFile`
  - **Sends:** `file_name`
  - **Purpose:** Extract Excel columns  

---

# Function Breakdown (Execution-Oriented)

Functions are grouped in roughly the **order they are encountered and used in real workflows**.

---

## 1. Initialization & Core UI

- `initializeChatStartupView`
  - Sets initial UI state (focus, scroll)

- `sendUserMessage`
  - Entry point for user interaction  
  - Routes commands to workflows  

- `addMessage`
- `addLiveMessage`
- `createLiveStatusMessage`
- `scrollMessagesToBottom`
  - Core chat UI rendering utilities  

---

## 2. User Interaction Helpers

- `addConfirmation`
- `addThreeOptionConfirmation`
  - Prompt user decisions (yes/no or multi-option)

- `triggerFileInput`
  - Opens file picker

---

## 3. Local Storage & File Persistence

- `openFilesDB`
  - Initializes IndexedDB

- `saveFileToIndexedDB`
  - Stores file blobs

- `getFileFromIndexedDB`
  - Retrieves stored files

- `getSavedAnalyzeConfigMeta`
  - Reads saved config metadata

- `setSavedAnalyzeConfigMeta` ⚠️ **UNUSED**
  - Legacy save logic (safe to remove)

---

## 4. File Upload & Preview Flow

- `uploadAndProcess`
  - Upload file to backend  
  - Trigger change detection  

- `parseAndRenderFromArrayBuffer`
- `parseAndRenderFromText`
- `renderTableFromRows`
  - File parsing + preview rendering  

---

## 5. Change Detection

- `runAutoChangeDetectionForUpload`
  - Runs detection after upload  

- `summarizeSheetChanges`
- `summarizeRowChanges`
  - Build human-readable summaries  

- `isNewFileChangeImpact`
  - Helper for new-file logic  

- `showChangeResults`
  - Displays results in UI  

---

## 6. API Layer

- `callApi`
  - Standard API wrapper with UI output  

- `callApiSilent`
  - Silent version (internal use)  

- `formatLog`
  - Formats backend logs  

---

## 7. Config Selection & Management

- `promptAnalyzeConfigChoice`
- `promptChooseConfigFile`
- `promptChooseConfigFileForDir`
  - User-driven config selection  

- `uploadConfigToConfigDir`
  - Upload config to backend  

- `fetchAnalyzeConfigOptions`
  - Retrieve config list  

---

## 8. Analysis Pipeline (CORE SYSTEM)

- `startAnalyze`
  - Entry point for analysis  

- `analyzeFiles`
  - Handles two-file upload flow  

- `analyzePreparedInputFile`
  - Prepares + sends analysis request  
  - Supports:
    - precomputation
    - prepare-only mode
    - timeout handling  

- `runConfirmedPreparedAnalyze`
  - Executes analysis after user confirmation  

---

### Queue System (Important)

- `runPreparedAnalysisQueue`
  - Guided execution queue  
  - Handles:
    - step progression  
    - config resolution  
    - user prompts  

- `runPreparedAnalysisQueueAuto`
  - Fully automated version  

- `flushChatUi`
  - Ensures UI updates during queue execution  

---

## 9. Response Processing Pipeline

- `runResponseProcessingWorkflow`
  - Main entry point  

- `processResponseQueue`
  - Iterates response files  

- `fetchPendingResponses`
  - Retrieves pending responses  

- `archiveResponseRecord`
  - Archives completed records  

---

## 10. Smart Update System

- `executeAutoHandleResponse`
  - Runs auto-handling logic  

- `appendMappingsForRecord`
  - Appends config mappings  

- `previewSmartUpdateForRecord`
  - Generates update proposals  

- `applySmartUpdateForRecord`
  - Applies accepted updates  

- `createSmartUpdateTable`
  - UI rendering  

- `promptSmartUpdateReview`
  - User approval flow  

---

## 11. File Processing Commands

- `runFileProcessing`
  - Full pipeline execution  

- `runValidateOnly`
- `runTransformOnly`
- `runCleanup`
  - Individual pipeline steps  

---

## 12. External Integrations

- `runOneDriveDownload`
- `startOneDriveAuth`
- `completeOneDriveAuth`
- `runOneDriveAuthSetup`
- `runFileSync`
- `runGoogleToOneDrive`

---

## 13. Column Extraction

- `extractColumnsFromExcel`
  - Prompts user selection  

- `extractColumnsFromSelectedFile`
  - Calls backend extraction  

---

## 14. Misc Utilities

- `showHelp`
- `toggleDebugMode`

---

# Unused / Legacy Functions

These are present but not actively used in the current frontend:

- `setSavedAnalyzeConfigMeta`
- `runExcelScripts` (deprecated alias)

These are safe candidates for removal unless future features depend on them.

---

# Key Architecture Notes

- The app is **workflow-driven**, not route/page-based  
- Most logic is controlled via:
  - global state flags
  - user prompts in chat UI  

- The **analysis queue system** is the most complex part:
  - handles orchestration
  - manages dependencies
  - interacts with multiple APIs  

- The frontend acts as a **controller layer**, coordinating:
  - file ingestion  
  - backend processing  
  - AI analysis  
  - response handling  

---

# Summary

This file represents a **full orchestration layer** between the user and backend systems.

Understanding these areas is critical:
- Upload + change detection
- Analysis queue system
- Response processing workflow

These three systems define how the application behaves end-to-end.