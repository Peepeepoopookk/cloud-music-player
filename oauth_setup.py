import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow

# Define the scopes
SCOPES = ['https://www.googleapis.com/auth/drive']

def main():
    # 1. Load credentials
    credentials_path = 'oauth_credentials.json'
    if not os.path.exists(credentials_path):
        print(f"Error: {credentials_path} not found in project root.")
        return
        
    # 2. Run the InstalledAppFlow
    print("Starting OAuth 2.0 authorization flow...")
    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    
    # Run local server to complete the auth flow
    # This will open a browser window
    creds = flow.run_local_server(port=0)
    
    # 3. Save the token to token.json
    token_path = 'token.json'
    with open(token_path, 'w') as token_file:
        token_file.write(creds.to_json())
        
    # 4. Print success message
    print(f"Success! Credentials saved to {token_path}")

if __name__ == '__main__':
    main()
