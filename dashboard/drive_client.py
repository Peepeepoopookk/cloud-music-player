import os
import json
import io
import logging
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload, MediaFileUpload
from googleapiclient.errors import HttpError

# Configure logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

SCOPES = ['https://www.googleapis.com/auth/drive']

def _initialize_oauth_from_env():
    """
    Checks for OAUTH_TOKEN and OAUTH_CREDENTIALS environment variables.
    If present and non-empty, writes them to token.json and oauth_credentials.json 
    in the project root, enabling environments like Render to authenticate via env vars.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    oauth_token = os.environ.get('OAUTH_TOKEN')
    if oauth_token and oauth_token.strip():
        token_path = os.path.join(project_root, 'token.json')
        try:
            with open(token_path, 'w', encoding='utf-8') as f:
                f.write(oauth_token)
            logger.info("Wrote token.json from OAUTH_TOKEN environment variable")
        except Exception as e:
            logger.error(f"Failed to write token.json from environment variable: {e}")

    oauth_credentials = os.environ.get('OAUTH_CREDENTIALS')
    if oauth_credentials and oauth_credentials.strip():
        credentials_path = os.path.join(project_root, 'oauth_credentials.json')
        try:
            with open(credentials_path, 'w', encoding='utf-8') as f:
                f.write(oauth_credentials)
            logger.info("Wrote oauth_credentials.json from OAUTH_CREDENTIALS environment variable")
        except Exception as e:
            logger.error(f"Failed to write oauth_credentials.json from environment variable: {e}")

# Run initialization once on module import
_initialize_oauth_from_env()

def get_drive_service():
    """
    Builds and returns an authenticated Google Drive API v3 service object.
    Checks GOOGLE_SERVICE_ACCOUNT env var first, falling back to service_account.json in root.
    """
    credentials = None
    try:
        # 1. Try environment variable
        sa_env = os.environ.get('GOOGLE_SERVICE_ACCOUNT')
        if sa_env:
            try:
                info = json.loads(sa_env)
                credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=SCOPES
                )
                logger.info("Authenticated successfully using credentials from GOOGLE_SERVICE_ACCOUNT environment variable.")
            except Exception as e:
                logger.error(f"Failed to load credentials from GOOGLE_SERVICE_ACCOUNT environment variable: {e}")

        # 2. Try local fallback file
        if not credentials:
            # Look in the project root (assumed to be parent directory or current directory)
            # Checking root directory path or just 'service_account.json'
            fallback_paths = [
                'service_account.json',
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'service_account.json')
            ]
            for path in fallback_paths:
                if os.path.exists(path):
                    try:
                        credentials = service_account.Credentials.from_service_account_file(
                            path, scopes=SCOPES
                        )
                        logger.info(f"Authenticated successfully using credentials from local file: {path}")
                        break
                    except Exception as e:
                        logger.error(f"Failed to load credentials from file {path}: {e}")

        if not credentials:
            raise ValueError("No valid service account credentials found. Please set GOOGLE_SERVICE_ACCOUNT env var or provide service_account.json.")

        service = build('drive', 'v3', credentials=credentials)
        return service

    except Exception as e:
        logger.error(f"Error building Drive service client: {e}", exc_info=True)
        raise

def get_oauth_drive_service():
    """
    Builds and returns an authenticated Google Drive API v3 service object using OAuth 2.0.
    If the token is expired, refreshes it automatically and saves it.
    Falls back to service account authentication if OAuth is unavailable.
    """
    credentials = None
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    token_paths = [
        'token.json',
        os.path.join(project_root, 'token.json')
    ]
    
    token_path = None
    for path in token_paths:
        if os.path.exists(path):
            token_path = path
            break
            
    if token_path:
        try:
            credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
            if credentials and not credentials.valid:
                if credentials.expired and credentials.refresh_token:
                    logger.info("OAuth token is expired, refreshing automatically...")
                    credentials.refresh(Request())
                    with open(token_path, 'w') as token_file:
                        token_file.write(credentials.to_json())
                    logger.info("OAuth token refreshed and saved successfully.")
        except Exception as e:
            logger.error(f"Failed to load or refresh OAuth credentials from {token_path}: {e}")
            credentials = None
            
    if credentials and credentials.valid:
        try:
            service = build('drive', 'v3', credentials=credentials)
            logger.info("Authenticated successfully using OAuth credentials.")
            return service
        except Exception as e:
            logger.error(f"Failed to build Drive service using OAuth credentials: {e}")
            
    logger.info("OAuth authentication unavailable or invalid. Falling back to Service Account.")
    return get_drive_service()

def list_files(folder_id=None):
    """
    Lists all files in a given Drive folder.
    Returns a list of dicts with id, name, size, mimeType, createdTime.
    """
    try:
        service = get_oauth_drive_service()
        query = "trashed = false"
        if folder_id:
            query += f" and '{folder_id}' in parents"
        
        logger.info(f"Listing files with query: {query}")
        results = service.files().list(
            q=query,
            fields="files(id, name, size, mimeType, createdTime)",
            pageSize=100
        ).execute()
        
        files = results.get('files', [])
        logger.info(f"Successfully retrieved {len(files)} files.")
        return files
    except HttpError as error:
        logger.error(f"Google API HttpError in list_files: {error}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in list_files: {e}", exc_info=True)
        raise

def search_file_by_name(filename, parent_id=None):
    """
    Searches for a file by exact name within an optional parent folder.
    Returns the file ID if found, otherwise None.
    """
    try:
        service = get_oauth_drive_service()
        query = f"name = '{filename}' and trashed = false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        
        logger.info(f"Searching for file with query: {query}")
        results = service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=1
        ).execute()
        
        files = results.get('files', [])
        if files:
            logger.info(f"Found file '{filename}' with ID: {files[0].get('id')}")
            return files[0].get('id')
        return None
    except HttpError as error:
        logger.error(f"Google API HttpError in search_file_by_name: {error}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Unexpected error in search_file_by_name: {e}", exc_info=True)
        return None

def download_json(file_id):
    """
    Downloads and parses a JSON file from Drive, returning a Python dict.
    """
    try:
        service = get_oauth_drive_service()
        logger.info(f"Downloading JSON file with ID: {file_id}")
        request = service.files().get_media(fileId=file_id)
        
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            
        fh.seek(0)
        content = fh.read().decode('utf-8')
        data = json.loads(content)
        logger.info(f"Successfully downloaded and parsed JSON file {file_id}.")
        return data
    except HttpError as error:
        logger.error(f"Google API HttpError in download_json: {error}", exc_info=True)
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse downloaded file as JSON: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in download_json: {e}", exc_info=True)
        raise

def upload_json(file_id, data, filename, parent_id=None):
    """
    Uploads/updates a JSON file on Drive with new data.
    If file_id is provided, updates the existing file. Otherwise, creates a new one.
    """
    try:
        service = get_oauth_drive_service()
        json_str = json.dumps(data, indent=2)
        media = MediaInMemoryUpload(json_str.encode('utf-8'), mimetype='application/json', resumable=True)
        
        if file_id:
            logger.info(f"Updating existing JSON file with ID: {file_id}")
            file = service.files().update(
                fileId=file_id,
                body={'name': filename},
                media_body=media,
                fields='id, name'
            ).execute()
            logger.info(f"Successfully updated JSON file: {file.get('name')} (ID: {file.get('id')})")
            return file
        else:
            logger.info(f"Creating new JSON file: {filename}")
            file_metadata = {
                'name': filename,
                'mimeType': 'application/json'
            }
            if parent_id:
                file_metadata['parents'] = [parent_id]
            file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, name'
            ).execute()
            logger.info(f"Successfully created JSON file: {file.get('name')} (ID: {file.get('id')})")
            return file
    except HttpError as error:
        logger.error(f"Google API HttpError in upload_json: {error}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in upload_json: {e}", exc_info=True)
        raise

def upload_media(file_path, folder_id, filename):
    """
    Uploads a media file to a specific Drive folder.
    Returns the new file's Drive ID.
    """
    try:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Local file not found: {file_path}")
            
        service = get_oauth_drive_service()
        logger.info(f"Uploading media file {file_path} to folder {folder_id} as {filename}")
        
        media = MediaFileUpload(file_path, mimetype='application/octet-stream', resumable=True)
        file_metadata = {
            'name': filename
        }
        if folder_id:
            file_metadata['parents'] = [folder_id]
            
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        new_id = file.get('id')
        logger.info(f"Successfully uploaded media. New Drive ID: {new_id}")
        return new_id
    except HttpError as error:
        logger.error(f"Google API HttpError in upload_media: {error}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in upload_media: {e}", exc_info=True)
        raise

def delete_file(file_id):
    """
    Deletes a file from Drive by its ID.
    """
    try:
        service = get_oauth_drive_service()
        logger.info(f"Deleting file with ID: {file_id}")
        service.files().delete(fileId=file_id).execute()
        logger.info(f"Successfully deleted file with ID: {file_id}")
    except HttpError as error:
        logger.error(f"Google API HttpError in delete_file: {error}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in delete_file: {e}", exc_info=True)
        raise

def get_storage_quota():
    """
    Retrieves the storage quota from Google Drive using the about.get endpoint.
    """
    try:
        service = get_oauth_drive_service()
        logger.info("Retrieving storage quota from Google Drive about.get...")
        about = service.about().get(fields="storageQuota").execute()
        return about.get('storageQuota', {})
    except HttpError as error:
        logger.error(f"Google API HttpError in get_storage_quota: {error}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in get_storage_quota: {e}", exc_info=True)
        raise

def get_file_metadata(file_id):
    """
    Retrieves the metadata (like modifiedTime and size) of a file from Google Drive.
    """
    try:
        service = get_oauth_drive_service()
        logger.info(f"Retrieving metadata for file ID: {file_id}")
        meta = service.files().get(fileId=file_id, fields="id, name, modifiedTime, size").execute()
        return meta
    except HttpError as error:
        logger.error(f"Google API HttpError in get_file_metadata: {error}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in get_file_metadata: {e}", exc_info=True)
        raise


def get_valid_access_token():
    """
    Returns a valid OAuth 2.0 access token string, refreshing it automatically if expired.
    Falls back to service account credentials if OAuth is unavailable.
    """
    credentials = None
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    token_paths = [
        'token.json',
        os.path.join(project_root, 'token.json')
    ]
    
    token_path = None
    for path in token_paths:
        if os.path.exists(path):
            token_path = path
            break
            
    if token_path:
        try:
            credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
            if credentials:
                if not credentials.valid:
                    if credentials.expired and credentials.refresh_token:
                        logger.info("OAuth token is expired, refreshing automatically...")
                        credentials.refresh(Request())
                        with open(token_path, 'w') as token_file:
                            token_file.write(credentials.to_json())
                        logger.info("OAuth token refreshed and saved successfully.")
                if credentials.valid:
                    return credentials.token
        except Exception as e:
            logger.error(f"Failed to load or refresh OAuth credentials from {token_path}: {e}")
            credentials = None

    # Fallback to Service Account
    try:
        sa_env = os.environ.get('GOOGLE_SERVICE_ACCOUNT')
        if sa_env:
            try:
                info = json.loads(sa_env)
                credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=SCOPES
                )
            except Exception as e:
                logger.error(f"Failed to load credentials from GOOGLE_SERVICE_ACCOUNT env: {e}")

        if not credentials:
            fallback_paths = [
                'service_account.json',
                os.path.join(project_root, 'service_account.json')
            ]
            for path in fallback_paths:
                if os.path.exists(path):
                    try:
                        credentials = service_account.Credentials.from_service_account_file(
                            path, scopes=SCOPES
                        )
                        break
                    except Exception as e:
                        logger.error(f"Failed to load credentials from file {path}: {e}")

        if credentials:
            if not credentials.valid:
                credentials.refresh(Request())
            return credentials.token
    except Exception as e:
        logger.error(f"Failed to get service account access token: {e}")

    return None


def refresh_and_get_access_token():
    """
    Forces a refresh of the credentials (saving the refreshed credentials to token.json if using OAuth)
    and returns the new token string.
    """
    credentials = None
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    token_paths = [
        'token.json',
        os.path.join(project_root, 'token.json')
    ]
    
    token_path = None
    for path in token_paths:
        if os.path.exists(path):
            token_path = path
            break
            
    if token_path:
        try:
            credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
            if credentials:
                logger.info("Force refreshing OAuth credentials...")
                credentials.refresh(Request())
                with open(token_path, 'w') as token_file:
                    token_file.write(credentials.to_json())
                logger.info("OAuth token refreshed and saved successfully during retry.")
                return credentials.token
        except Exception as e:
            logger.error(f"Failed to force refresh OAuth credentials from {token_path}: {e}")
            credentials = None

    # Fallback to Service Account
    try:
        sa_env = os.environ.get('GOOGLE_SERVICE_ACCOUNT')
        if sa_env:
            try:
                info = json.loads(sa_env)
                credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=SCOPES
                )
            except Exception as e:
                logger.error(f"Failed to load credentials from GOOGLE_SERVICE_ACCOUNT env: {e}")

        if not credentials:
            fallback_paths = [
                'service_account.json',
                os.path.join(project_root, 'service_account.json')
            ]
            for path in fallback_paths:
                if os.path.exists(path):
                    try:
                        credentials = service_account.Credentials.from_service_account_file(
                            path, scopes=SCOPES
                        )
                        break
                    except Exception as e:
                        logger.error(f"Failed to load credentials from file {path}: {e}")

        if credentials:
            logger.info("Force refreshing Service Account credentials...")
            credentials.refresh(Request())
            return credentials.token
    except Exception as e:
        logger.error(f"Failed to force refresh service account access token: {e}")

    return None


