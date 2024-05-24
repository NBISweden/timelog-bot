import argparse
import yaml
import json
import os
import requests
from datetime import datetime
import smtplib
from email.mime.text import MIMEText

def load_config(config_file):
    with open(config_file, 'r') as file:
        return yaml.safe_load(file)

def save_state(state, file_path):
    with open(file_path, 'w') as f:
        json.dump(state, f)

def load_state(file_path):
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return json.load(f)
    return {}

def setup_argparse():
    parser = argparse.ArgumentParser(description='Redmine and Confluence Time Logging Bot')
    parser.add_argument('--config', type=str, required=True, help='Path to the config file')
    parser.add_argument('--dry-run', action='store_true', help='Run the script without making any changes')
    parser.add_argument('--update-confluence', action='store_true', help='Force update Confluence pages')
    parser.add_argument('--debug-space', type=str, help='Limit updating to a specific Confluence space for debugging')
    return parser.parse_args()

def fetch_time_entries(base_url, project_id):
    headers = {'X-Redmine-API-Key': config["redmine"]["api_key"]}
    time_entries = []
    offset = 0
    limit = 100

    while True:
        response = requests.get(f'{config["redmine"]["base_url"]}/time_entries.json?project_id={project_id}&offset={offset}&limit={limit}', headers=headers)
        if response.status_code != 200:
            raise Exception(f"Failed to fetch time entries: {response.content}")

        data = response.json()
        time_entries.extend(data['time_entries'])

        if len(data['time_entries']) < limit:
            break

        offset += limit
    return time_entries

def fetch_issues(config, project_id):
    headers = {'X-Redmine-API-Key': config["redmine"]["api_key"]}
    issues = []
    offset = 0
    limit = 100

    while True:
        response = requests.get(f'{config["redmine"]["base_url"]}/issues.json?project_id={project_id}&offset={offset}&limit={limit}', headers=headers)
        if response.status_code != 200:
            raise Exception(f"Failed to fetch issues: {response.content}")

        data = response.json()
        issues.extend(data['issues'])

        if len(data['issues']) < limit:
            break

        offset += limit
    return issues

def update_confluence(config, wabi_id, total_hours, monthly_hours):
    base_url = config["confluence"]["base_url"]
    
    headers = {
        'Authorization': f'Bearer {config["confluence"]["api_key"]}',
        'Content-Type': 'application/json'
    }
    
    space_name = f"NBIS {wabi_id}"
    page_title = "TimeLogBot"
    
    response = requests.get(f"{base_url}/rest/api/content?title={page_title}&spaceKey={space_name}", headers=headers)
    if response.status_code == 200:
        page_data = response.json()
        if page_data['size'] > 0:
            page_id = page_data['results'][0]['id']
            version = page_data['results'][0]['version']['number']
            
            page_content_response = requests.get(f"{base_url}/rest/api/content/{page_id}?expand=body.storage", headers=headers)
            if page_content_response.status_code == 200:
                page_content = page_content_response.json()
                existing_body = page_content['body']['storage']['value']
                
                parts = existing_body.split("<hr />")
                if len(parts) > 1:
                    head = parts[0]
                else:
                    head = existing_body

                new_body = f"{head}<hr /><p>Total Hours: {total_hours}</p><table>...</table>"
                
                data = {
                    'id': page_id,
                    'type': 'page',
                    'title': page_title,
                    'body': {
                        'storage': {
                            'value': new_body,
                            'representation': 'storage'
                        }
                    },
                    'version': {
                        'number': version + 1
                    }
                }
                
                update_response = requests.put(f"{base_url}/rest/api/content/{page_id}", headers=headers, json=data)
                return update_response.status_code == 200
    else:
        data = {
            'type': 'page',
            'title': page_title,
            'space': {
                'key': space_name
            },
            'body': {
                'storage': {
                    'value': f"<h1>{page_title}</h1><hr /><p>Total Hours: {total_hours}</p><table>...</table>",
                    'representation': 'storage'
                }
            }
        }
        
        create_response = requests.post(f"{base_url}/rest/api/content/", headers=headers, json=data)
        return create_response.status_code == 200
    return False

def send_email(config, to_emails, subject, body):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = config['smtp']['from']
    msg['To'] = ', '.join(to_emails)

    server = smtplib.SMTP(config['smtp']['server'], config['smtp']['port'])
    server.starttls()
    server.login(config['smtp']['from'], config['smtp']['password'])
    server.sendmail(config['smtp']['from'], to_emails, msg.as_string())
    server.quit()

def main():
    args = setup_argparse()
    config = load_config(args.config)

    script_dir = os.path.dirname(os.path.realpath(__file__))
    state_file_path = os.path.join(script_dir, 'state.json')
    state = load_state(state_file_path)
    
    redmine_api_key = config['redmine_api_key']
    confluence_api_key = config['confluence_api_key']

    for group in config['project_groups']:
        project_ids = group['ids']
        managers = group['managers']
        checkpoints = group['checkpoints']
        update_pages = group['confluence']['update_pages']

        for project_id in project_ids:
            issues = fetch_issues(redmine_api_key, project_id)
            for issue in issues:
                time_entries = fetch_time_entries(redmine_api_key, issue['id'])
                total_hours = sum(entry['hours'] for entry in time_entries)

                wabi_id = next((cf['value'] for cf in issue['custom_fields'] if cf['name'] == 'WABI ID'), None)
                if wabi_id:
                    if update_pages:
                        update_confluence(confluence_api_key, wabi_id, total_hours, None)  # Monthly hours calculation is omitted for simplicity
                        
                if total_hours > issue['estimated_hours']:
                    send_email(config['smtp']['server'], config['smtp']['port'], config['smtp']['from'], config['smtp']['password'], managers, "Estimate Exceeded", f"Issue {issue['id']} exceeded estimated hours.")

    save_state(state, state_file_path)

if __name__ == '__main__':
    main()

