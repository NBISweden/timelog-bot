import argparse
import yaml
import os
import requests
from datetime import datetime
from collections import defaultdict
import smtplib
from email.mime.text import MIMEText
import logging

import pdb
from pprint import pprint


# global logger instance, initialised in setup_logging()
logger = None


def load_config(config_file):
    """
    Load the configuration from a YAML file.
    :param config_file: Path to the configuration file
    :return: Configuration dictionary
    """
    with open(config_file, 'r') as file:
        return yaml.safe_load(file)


def save_state(state, file_path):
    """
    Save the state to a YAML file.
    :param state: State dictionary
    :param file_path: Path to the state file
    """
    with open(file_path, 'w') as f:
        yaml.dump(state, f)


def load_state(file_path):
    """
    Load the state from a YAML file.
    :param file_path: Path to the state file
    :return: State dictionary, or empty dict if the file does not exist
    """
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return yaml.safe_load(f)
    return {}


def setup_argparse():
    """
    Set up command line argument parsing.
    :return: Parsed arguments
    """
    parser = argparse.ArgumentParser(description='Redmine and Confluence Time Logging Bot')
    parser.add_argument('--config', '-c', type=str, required=True, help='Path to the config file')
    parser.add_argument('--debug', '-d', action='store_true', help='Enable debug messages')
    parser.add_argument('--debug-issue-id', type=int, help='Limit updating to a specific Redmine ticket for debugging, e.g. 1234')
    parser.add_argument('--debug-space', type=str, help='Limit updating to a specific Confluence space for debugging')
    parser.add_argument('--disable-email', '-e', action='store_true', help='Emailing disabled')
    parser.add_argument('--dry-run', '-n', action='store_true', help='Run the script without making any changes')
    parser.add_argument('--force-update', '-f', action='store_true', help='Force processing even if hours have not changed')
    parser.add_argument('--force-save-state', '-s', action='store_true', help='Force saving of the state, even if dry-run or debug options are used')
    parser.add_argument('--state-file', type=str, help='Path to state file (default: state.yaml in script directory)')
    return parser.parse_args()


def setup_logging():
    """
    Set up logging configuration.
    :return: Logger object
    """
    global logger
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)
    return logger


def fetch_time_entries(config, issue_id):
    """
    Fetch all time entries from Redmine for a given issue ID, handling pagination.
    :param config: Configuration dictionary
    :param issue_id: Issue ID to fetch time entries for
    :return: List of time entry dictionaries
    """
    headers = {'X-Redmine-API-Key': config["redmine"]["api_key"]}
    time_entries = []
    offset = 0
    limit = 100

    while True:
        response = requests.get(
            f'{config["redmine"]["base_url"]}/time_entries.json?issue_id={issue_id}&offset={offset}&limit={limit}',
            headers=headers
        )
        if response.status_code != 200:
            raise Exception(f"Failed to fetch time entries: {response.content}")

        data = response.json()
        time_entries.extend(data['time_entries'])

        # stop when we receive fewer entries than requested — that means we are on the last page
        if len(data['time_entries']) < limit:
            break

        offset += limit

    return time_entries


def fetch_issues(config, project_id):
    """
    Fetch all open issues from Redmine for a given project ID, handling pagination.
    :param config: Configuration dictionary
    :param project_id: Project ID to fetch issues for
    :return: List of issue dictionaries
    """
    headers = {'X-Redmine-API-Key': config["redmine"]["api_key"]}
    issues = []
    offset = 0
    limit = 100

    while True:
        response = requests.get(
            f'{config["redmine"]["base_url"]}/issues.json?project_id={project_id}&status_id=open&offset={offset}&limit={limit}',
            headers=headers
        )
        if response.status_code != 200:
            raise Exception(f"Failed to fetch issues: {response.content}")

        data = response.json()
        issues.extend(data['issues'])

        # stop when we receive fewer issues than requested — that means we are on the last page
        if len(data['issues']) < limit:
            break

        offset += limit

    return issues


def update_confluence(args, config, issue, name2key, total_hours, time_entries):
    """
    Update the Confluence TimeLog page for a given issue with the latest hour totals.
    Creates the page if it does not already exist. Any user-written content above the
    first <hr /> separator is preserved on update.
    :param args: Command line arguments
    :param config: Configuration dictionary
    :param issue: Redmine issue dictionary
    :param name2key: Dict mapping Confluence space names to their space metadata
    :param total_hours: Total hours logged on the issue
    :param time_entries: List of time entry dictionaries for the issue
    :return: True if the page was successfully created or updated, False otherwise
    """
    base_url = config["confluence"]["base_url"]
    headers = {"Accept": "application/json"}
    auth = requests.auth.HTTPBasicAuth(config["confluence"]["username"], config["confluence"]["api_key"])

    # read WABI ID and hours ordered from Redmine custom fields
    support_project_id = next((cf['value'] for cf in issue['custom_fields'] if cf['name'] == 'WABI ID'), '')
    hours_ordered = float(next((cf['value'] for cf in issue['custom_fields'] if cf['name'] == 'Hours ordered' and cf['value']), -1))

    # allow overriding the target space when debugging
    if args.debug_space:
        logger.info(f"Using debug space: {args.debug_space}")
        space_name = args.debug_space
    else:
        space_name = f"NBIS {support_project_id}"

    # resolve space name to its Confluence key
    try:
        space_key = name2key.get(space_name)[0]['key']
    except (TypeError, IndexError):
        logger.error(f"Space \"{space_name}\" not found in Confluence, skipping update for WABI ID \"{support_project_id}\".")
        return False

    page_title = "TimeLog"

    # check whether the page already exists in this space
    response = requests.get(
        f"{base_url}/wiki/rest/api/content?title={page_title}&spaceKey={space_key}&expand=version",
        headers=headers, auth=auth
    )
    if response.status_code != 200:
        logger.error(f"Failed to query Confluence for page '{page_title}' in space '{space_name}': {response.content}")
        return False

    page_data = response.json()

    # build the hours-by-month table rows, shared by both the create and update paths
    table_rows = ""
    for monthly in reversed(summarize_time_entries_by_month(time_entries)):
        table_rows += f"<tr><td>{monthly['month']}</td><td>{round(monthly['hours'], 2)}</td></tr>\n"

    if page_data['size'] > 0:
        # page exists — fetch its body so we can preserve the user-written section above <hr />
        page_id = page_data['results'][0]['id']
        version = page_data['results'][0]['version']['number']

        page_content_response = requests.get(
            f"{base_url}/wiki/rest/api/content/{page_id}?expand=body.storage",
            headers=headers, auth=auth
        )
        if page_content_response.status_code != 200:
            logger.error(f"Failed to fetch content for page {page_id}: {page_content_response.content}")
            return False

        existing_body = page_content_response.json()['body']['storage']['value']

        # keep everything above the first <hr /> so user-written text is not overwritten
        head = existing_body.split("<hr />")[0]

        new_body = (
            f"{head}"
            f"<hr />"
            f"<h2>Project {support_project_id} is {round(total_hours / hours_ordered * 100, 2)}% complete</h2>"
            f"<p>{round(total_hours, 2)} out of {int(hours_ordered)} hours spent.</p>"
            f"<p><table><tr><th>Date</th><th>Hours spent</th></tr>"
            f"{table_rows}"
            f"</table></p>"
        )

        data = {
            'id': page_id,
            'type': 'page',
            'title': page_title,
            'body': {'storage': {'value': new_body, 'representation': 'storage'}},
            'version': {'number': version + 1}
        }

        if args.dry_run:
            print(f"Dry run: Would have updated page {base_url}/wiki/rest/api/content/{page_id}")
            return True

        update_response = requests.put(
            f"{base_url}/wiki/rest/api/content/{page_id}",
            headers=headers, json=data, auth=auth
        )
        return update_response.status_code == 200

    else:
        # page does not exist — create it with no pre-existing user header
        new_body = (
            f"<hr />"
            f"<h2>Project {support_project_id} is {round(total_hours / hours_ordered * 100, 2)}% complete</h2>"
            f"<p>{total_hours} out of {hours_ordered} used.</p>"
            f"<p><table><tr><th>Date</th><th>Hours spent</th></tr>"
            f"{table_rows}"
            f"</table></p>"
        )

        data = {
            'type': 'page',
            'title': page_title,
            'space': {'key': space_key},
            'body': {'storage': {'value': new_body, 'representation': 'storage'}}
        }

        if args.dry_run:
            print(f"Dry run: Would have created page '{page_title}' in space '{space_name}'")
            return True

        create_response = requests.post(
            f"{base_url}/wiki/rest/api/content/",
            headers=headers, json=data, auth=auth
        )
        return create_response.status_code == 200


def summarize_time_entries_by_month(time_entries):
    """
    Summarize time entries by month.
    :param time_entries: List of time entry dictionaries
    :return: List of dicts with 'month' (e.g. "May 2025") and 'hours' keys, sorted chronologically
    """
    monthly_summary = defaultdict(float)

    for entry in time_entries:
        # skip malformed entries that are missing required fields
        if 'spent_on' not in entry or 'hours' not in entry:
            continue

        spent_date = datetime.strptime(entry['spent_on'], '%Y-%m-%d')
        month_key = spent_date.strftime('%Y-%m')  # e.g. "2025-05" — used as sort key
        monthly_summary[month_key] += float(entry['hours'])

    result = []
    for month_key in sorted(monthly_summary.keys()):
        date_obj = datetime.strptime(month_key, '%Y-%m')
        result.append({
            'month': date_obj.strftime('%B %Y'),  # human-readable, e.g. "May 2025"
            'hours': monthly_summary[month_key]
        })

    return result


def send_email(config, args, to_emails, subject, body):
    """
    Send an email notification via SMTP.
    Does nothing when --disable-email or --dry-run is set.
    :param config: Configuration dictionary
    :param args: Command line arguments
    :param to_emails: List of recipient email addresses
    :param subject: Subject of the email
    :param body: Plain-text body of the email
    """
    if args.disable_email:
        logger.info(f"Emailing is disabled, not sending email \"{subject}\"")
        return
    if args.dry_run:
        logger.info(f"Dry run: Would have sent email \"{subject}\"")
        return

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = config['smtp']['from']
    msg['To'] = ', '.join(to_emails)

    server = smtplib.SMTP(config['smtp']['server'], config['smtp']['port'])
    server.starttls()
    server.login(config['smtp']['from'], config['smtp']['password'])
    server.sendmail(config['smtp']['from'], to_emails, msg.as_string())
    server.quit()


def fetch_space_list(config, args):
    """
    Fetch all Confluence spaces and return them indexed by display name.
    Handles pagination automatically. Warns if multiple spaces share the same name.
    :param config: Configuration dictionary
    :return: Dict mapping space name -> list of space objects (normally a single-element list)
    """
    base_url = config["confluence"]["base_url"]
    headers = {"Accept": "application/json"}
    auth = requests.auth.HTTPBasicAuth(config["confluence"]["username"], config["confluence"]["api_key"])

    space_dict = {}
    start = 0
    limit = 50

    while True:
        response = requests.get(
            f"{base_url}/wiki/rest/api/space?limit={limit}&start={start}",
            headers=headers, auth=auth
        )
        if response.status_code != 200:
            raise Exception(f"Failed to fetch spaces: {response.content}")

        data = response.json()

        for space in data['results']:
            if space['name'] in space_dict:
                # warn: two spaces with the same display name makes the lookup ambiguous
                logger.warning(
                    f"Duplicate space name found: \"{space['name']}\", "
                    f"used in {', '.join([s['key'] for s in space_dict[space['name']]] + [space['key']])}"
                )
                space_dict[space['name']].append(space)
            else:
                space_dict[space['name']] = [space]

        # stop when we receive fewer spaces than requested — that means we are on the last page
        if len(data['results']) < limit:
            break

        start += limit

    return space_dict


def main():

    args = setup_argparse()
    logger = setup_logging()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug mode enabled")

    logger.debug("Loading configuration")
    config = load_config(args.config)

    if args.state_file:
        state_file_path = args.state_file
    else:
        script_dir = os.path.dirname(os.path.realpath(__file__))
        state_file_path = os.path.join(script_dir, 'state.yaml')

    logger.debug("Loading state")
    state = load_state(state_file_path)

    # fetch all Confluence spaces once up front to avoid per-issue API calls
    name2key = fetch_space_list(config, args)

    for group in config['project_groups']:
        logger.debug(f"Processing project group: {group['group_name']} ({', '.join(group['ids'])})")

        project_ids = group['ids']
        managers = group['managers']
        checkpoints = group['checkpoints']
        update_pages = group['confluence']['update_pages']

        for project_id in project_ids:
            logger.info(f"Processing project ID: {project_id}")

            issues = fetch_issues(config, project_id)

            if args.debug_issue_id:
                logger.debug(f"Debugging specific issue ID: {args.debug_issue_id}, skipping other issues")

            for issue in issues:

                # when debugging a single ticket, skip all others
                if args.debug_issue_id:
                    if issue['id'] == args.debug_issue_id:
                        logger.debug(f"Debugging specific issue ID: {issue['id']}")
                    else:
                        continue

                logger.debug(f"Processing issue ID: {issue['id']}")

                time_entries = fetch_time_entries(config, issue['id'])
                total_hours = sum(entry['hours'] for entry in time_entries)

                # ensure issue ID is stored as a string for consistent state dict keys
                issue['id'] = str(issue['id'])
                logger.debug(f"Total hours for issue {issue['id']}: {total_hours}")

                # calculate how many days the issue has been open
                start_date = datetime.fromisoformat(issue['start_date'].replace('Z', '+00:00')).date()
                current_date = datetime.now().date()
                issue_age_days = (current_date - start_date).days

                hours_ordered = next((float(cf['value']) for cf in issue['custom_fields'] if cf['name'] == 'Hours ordered' and cf['value']), '')
                hours_percent = (total_hours / float(hours_ordered)) * 100 if hours_ordered else 0

                # first run for this issue: record baseline state and skip all notifications
                # to avoid a flood of alerts for issues that have already been running for a while
                if issue['id'] not in state:
                    state[issue['id']] = {
                        'total_hours': total_hours,
                        'warnings_sent': {
                            'hours_exceeded': total_hours >= hours_ordered if hours_ordered else False,
                            'checkpoint_passed_days': [c for c in checkpoints['days'] if issue_age_days >= c],
                            'checkpoint_passed_percent': [c for c in checkpoints['percent_hours'] if hours_percent >= c]
                        }
                    }
                    logger.debug(f"Skipping issue {issue['id']} as it is new (first run).")
                    continue

                # only update Confluence and check hour-based alerts when hours have actually changed
                if state[issue['id']]['total_hours'] != total_hours or args.force_update:

                    # update the Confluence page if this project group has that enabled
                    support_project_id = next((cf['value'] for cf in issue['custom_fields'] if cf['name'] == 'WABI ID'), None)
                    if support_project_id and update_pages:
                        logger.debug(f"Updating Confluence page for project id {support_project_id}")
                        update_confluence(args, config, issue, name2key, total_hours, time_entries)

                    # send a warning if total hours have now exceeded the ordered amount (only once per issue)
                    exceeded = hours_ordered and total_hours > float(hours_ordered)
                    if exceeded and not state[issue['id']]['warnings_sent']['hours_exceeded']:
                        logger.warning(f"Issue {issue['id']} has exceeded ordered hours: {total_hours} > {hours_ordered}")
                        send_email(
                            config, args, managers,
                            f"TimeLogBot: Hours ordered exceeded for #{issue['id']}: \"{issue['subject']}\"",
                            f"Issue {issue['id']} exceeded ordered hours."
                        )

                    # update the flag so we don't resend the alert if hours increase further
                    state[issue['id']]['warnings_sent']['hours_exceeded'] = bool(exceeded)

                # check age checkpoints regardless of whether hours changed
                if start_date:
                    newly_passed = [
                        cp for cp in group['checkpoints']['days']
                        if issue_age_days >= cp and cp not in state[issue['id']]['warnings_sent']['checkpoint_passed_days']
                    ]
                    if newly_passed:
                        checkpoint_days = max(newly_passed)
                        logger.warning(f"Issue {issue['id']} has passed {checkpoint_days} days since start date ({issue_age_days} days)")
                        send_email(
                            config, args, managers,
                            f"TimeLogBot: {issue_age_days} days issue age for #{issue['id']}: \"{issue['subject']}\"",
                            f"Issue {issue['id']} has been running for {issue_age_days} days (checkpoint: {checkpoint_days} days)."
                        )
                        state[issue['id']]['warnings_sent']['checkpoint_passed_days'].extend(newly_passed)

                # check percentage-of-hours-used checkpoints
                if hours_ordered:
                    newly_passed = [
                        cp for cp in group['checkpoints']['percent_hours']
                        if hours_percent >= cp and cp not in state[issue['id']]['warnings_sent']['checkpoint_passed_percent'] 
                    ]
                    if newly_passed:
                        checkpoint_percent = max(newly_passed)
                        logger.warning(f"Issue {issue['id']} has used {hours_percent:.1f}% of ordered hours (checkpoint: {checkpoint_percent}%)")
                        send_email(
                            config, args, managers,
                            f"TimeLogBot: {hours_percent:.1f}% of ordered hours used by #{issue['id']}: \"{issue['subject']}\"",
                            f"Issue {issue['id']} has used {hours_percent:.1f}% of ordered hours (checkpoint: {checkpoint_percent}%)."
                        )
                        state[issue['id']]['warnings_sent']['checkpoint_passed_percent'].extend(newly_passed)

                # when debugging a single ticket, stop here without persisting state changes
                if args.debug_issue_id:
                    logger.debug(f"Stopping after processing debug ticket id {args.debug_issue_id}")
                    break

                logger.debug("Saving issue state")
                state[issue['id']]['total_hours'] = total_hours
    
    if not args.force_save_state and args.dry_run:
        logger.info("Dry run enabled, not saving state")
    elif not args.force_save_state and (args.debug_issue_id or args.debug_space):
        logger.info("Debug mode enabled, not saving state")
    else:
        logger.debug("Saving final state")
        save_state(state, state_file_path)


if __name__ == '__main__':
    main()
