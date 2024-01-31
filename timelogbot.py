#!/usr/bin/env python3
"""
Automatically update Confluence pages in the SciLifeLab instance  with information from Redmine.

To run it, you need Python 3:

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python timelogbot.py config.ini
"""
__author__ = "Martin Dahlö, inherited from Marcel Martin"


from argparse import ArgumentParser
from collections import namedtuple
from configparser import ConfigParser
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from itertools import groupby
from redminelib import Redmine as Redmine_api
import json
import os
import re
import requests
import smtplib
import sqlite3
import ssl
import sys
import textwrap
import tomli

import pdb
from pprint import pprint

# Send notifications when a project reaches these work hours
CHECKPOINT_HOURS = (100, 300)

# Send a notification when a project reaches this number of days since the first logged hour
CHECKPOINT_DAYS = 365




class Database:
    """Persistently store the number of hours for each project"""

    def __init__(self, database_path):
        self.connection = sqlite3.connect(
            database_path, detect_types=sqlite3.PARSE_DECLTYPES
        )
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            "CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY NOT NULL, hours FLOAT, "
            "date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

    def __getitem__(self, name):
        self.cursor.execute("SELECT hours, date FROM projects WHERE name = ?", (name,))
        result = self.cursor.fetchone()
        if result is None:
            raise KeyError("project not in database")
        return result[0], result[1].date()

    def __setitem__(self, name, hours):
        self.cursor.execute(
            "INSERT OR REPLACE INTO projects (name, hours) VALUES (?, ?)", (name, hours)
        )

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.commit()
        self.connection.close()


class Confluence:
    def __init__(self, api_url, user, api_token, upload, force):
        """
        force -- do not skip generating new content if hours are unchanged
        upload -- whether to upload anything to Confluence (use for a 'dry run'
            option)
        """
        self.apiurl = api_url
        self.auth   = requests.auth.HTTPBasicAuth(user, api_token)
        self.upload = upload
        self.force  = force

    def find_pages(self, title="TimeLog"):
        """
        Search Confluence for all pages with the given title.
        Return a list of (id, space_name) tuples.
        TODO: Should handle pagination, will miss projects in the future when we hit 1000.
        """
        params = {"title": title, "expand": "space", "limit": 1000}
        r = requests.get(
            self.apiurl + "/content", params=params, auth=self.auth, timeout=20
        )
        r.raise_for_status()
        j = r.json()
        return [(page["id"], page["space"]["name"]) for page in j["results"]]

    def update_report_page(self, space_name, page_id, work_units, budget):
        """
        Update report in a Confluence page. The report is only updated if the
        numbers have changed.

        Return a bool indicating whether the report was updated.
        """
        url = self.apiurl + "/content/{id}".format(id=page_id)
        # Retrieve current page
        params = {"expand": "body.storage,version,ancestors"}
        j = requests.get(url, params=params, auth=self.auth).json()
        text = j["body"]["storage"]["value"]

        # Modify text to include the new report

        # The space in this constant is needed since the HTML gets cleaned up after uploading.
        marker = "<hr />"

        hours_spent = work_hours(work_units)
        index = text.find(marker)
        if index >= 0:
            if not self.force:
                # Find out whether we need to update the report at all
                previous_report = text[index:]
                m = re.search("([0-9.]+) out of ([0-9.]+) hours used", previous_report)
                if m is not None:
                    previous_hours_spent = float(m.group(1))
                    previous_budget = float(m.group(2))
                    if (
                        abs(hours_spent - previous_hours_spent) < 0.01
                        and abs(previous_budget - budget) < 0.01
                    ):
                        return False

            # Remove everything following the marker
            text = text[: text.index(marker)]

        # Create the report
        percent = hours_spent / budget if budget > 0 else 0.0

        # The <progress> tag gets filtered out unfortunately.
        # <progress value="{percent:.0%}" max="100"></progress>
        report = """
            {marker}
            <h2>Project {project_name} is {percent:.1%} complete</h2>
            <p>{hours:.2f} out of {budget:.0f} hours used.</p>
        """.format(
            marker=marker,
            project_name=space_name,
            percent=percent,
            hours=hours_spent,
            budget=budget,
        )
        report = textwrap.dedent(report)

        # Create the month report, most recent on top
        report += "<p><table>\n"
        report += "<tr><th>Date</th><th>Hours spent</th></tr>\n"
        for (year, month), group in groupby(
            sorted(work_units, key=lambda wu: wu['date'], reverse=True),
            key=lambda wu: (wu['date'].year, wu['date'].month),
        ):
            hours = sum(unit['hours'] for unit in group)
            formatted_date = date(year, month, 1).strftime("%B %Y")
            report += "<tr><td>{}</td><td>{:.2f}</td></tr>\n".format(
                formatted_date, hours
            )
        report += "</table></p>"

        # report += "<p><table><tr><td>a</td><td>b</td></tr></table></p>"
        # Build the new page content
        content = {
            "id": j["id"],
            "type": "page",
            "title": j["title"],
            "ancestors": j["ancestors"],
            "body": {
                "storage": {"value": text + "\n" + report, "representation": "storage"}
            },
            "version": {"number": j["version"]["number"] + 1},
        }

        # And upload it
        if self.upload:
            r = requests.put(
                url,
                auth=self.auth,
                data=json.dumps(content),
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
        return True


def get_config(path):
    with open(path, "rb") as f:
        raw_config = tomli.load(f)

    class Config:
        pass

    config = Config()
    config.redmine    = raw_config["redmine"]
    config.confluence = raw_config["confluence"]
    config.recipients = raw_config["recipients"]
    config.database   = raw_config["database"]
    config.email      = raw_config["email"]
    return config


class EMailer:
    def __init__(self, db, recipients, sender, host, port, user, password, dry_run=False):
        self.db         = db
        self.dry_run    = dry_run
        self.recipients = recipients
        self.sender     = sender
        self.host       = host
        self.user       = user
        self.password   = password
        self.port       = port

    def send_email(self, subject, body, force=False):
        message            = MIMEText(body)
        message["Subject"] = subject
        message["From"]    = self.sender
        message["To"]      = ", ".join(self.recipients)
        if self.dry_run and not force:
            print(
                "Dry-run active, not sending e-mail with subject {!r}".format(
                    message["Subject"]
                )
            )
            print("... and body:")
            print(body)
        else:
            context = ssl.create_default_context()
            s = smtplib.SMTP(self.host, port=self.port)
            s.starttls(context=context)
            s.login(self.user, self.password)
            s.send_message(message)
            s.quit()

    def update(self, name, hours, project_start_date):
        sent_email = False
        try:
            previous_hours, previous_date = self.db[name]
            previous_date = datetime.combine(previous_date, datetime.min.time()) # convert to datetime object
        except KeyError:
            previous_hours, previous_date = None, None
        elapsed_days = (datetime.today() - project_start_date).days
        if previous_hours is not None:
            for checkpoint in CHECKPOINT_HOURS:
                if previous_hours < checkpoint and hours >= checkpoint:
                    subject = "[TimeLog Bot] Checkpoint in project {}: {} hours".format(
                        name, checkpoint
                    )
                    body = "{} hours have been reached in project {}.\n".format(
                        hours, name
                    )
                    body += "{} calendar days have elapsed".format(elapsed_days)
                    self.send_email(subject, body)
                    sent_email = True
                    break
        if previous_date is not None and not sent_email:
            checkpoint = project_start_date + timedelta(days=CHECKPOINT_DAYS)
            if previous_date < checkpoint and datetime.today() >= checkpoint:
                subject = "[TimeLog Bot] Checkpoint in project {}: {} days".format(
                    name, CHECKPOINT_DAYS
                )
                body = "{} calendar days have been reached in project {}\n".format(
                    elapsed_days, name
                )
                body += "{:.1f} working hours have been spent".format(hours)
                self.send_email(subject, body)
                sent_email = True
        # Always do this to ensure the timestamp is updated even if the hours did not change
        self.db[name] = hours
        self.db.commit()
        return sent_email


def work_hours(units):
    return sum(work_unit['hours'] for work_unit in units)


def work_unit_to_json(unit):
    return {"date": unit['date'].strftime("%Y-%m-%d"), "hours": unit['hours']}


def normalize_project_id(project_id):
    """
    Replace special characters with URL friendly variants.
    """
    MAP = {
        "ö": "o",
        "ä": "a",
        "å": "a",
        "Ö": "O",
        "Ä": "A",
        "Å": "A",
    }
    return project_id.translate({ord(c): target for (c, target) in MAP.items()})


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--space", default=None, help="Confluence space to work on (default: all)"
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        default=False,
        help="Do not upload any new content to Confluence. Do not send e-mails",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Update even if nothing changed",
    )
    parser.add_argument(
        "--dump", metavar="FILE", help="Dump work units in JSON format to FILE"
    )
    parser.add_argument("configpath", help="Path to configuration file")
    args = parser.parse_args()
    config = get_config(args.configpath)

    db = Database(os.path.expanduser(config.database))
    emailer = EMailer(
        db, 
        config.recipients, 
        config.email["sender"], 
        config.email["host"], 
        config.email["port"], 
        config.email["user"], 
        config.email["password"], 
        args.dry_run
    )

    redmine = Redmine_api(config.redmine['url'], key=config.redmine['api_key'])

    # get the issues related to desired redmine projects
    projects = {}
    for redmine_project_id in config.redmine['projects']:

        # structure the data
        for project in redmine.issue.filter(project_id=redmine_project_id, status_id='*'):

            # get project id
            try:
                #pdb.set_trace()
                project_id = [attr['value'] for attr in project['_decoded_attrs']['custom_fields'] if attr['name'] == 'WABI ID'][0]
            except (IndexError, KeyError):
                project_id = None

            if project_id is None:

                # try using the subject name, if it follows the project id pattern (should this be removed after development?)
                if re.match(r'.*_\d{4}$', project['_decoded_attrs']['subject']):
                    project_id = project['_decoded_attrs']['subject']
                else:
#                    print(project['_decoded_attrs']['status'])
#                    if project['_decoded_attrs']['status']['name'] == 'In Progress':
#                        pdb.set_trace()
                    #pdb.set_trace()
                    print("(redmine id {}) {:>25s}: ignored, no WABI ID attribute or compatible issue subject found.".format(project['_decoded_attrs']['id'], project['_decoded_attrs']['subject']))
                    continue

            # save the project id with special characters removed
            projects[normalize_project_id(project_id)] = project


    # connect to confluence
    confluence = Confluence(
        config.confluence["api_url"],
        config.confluence["user"],
        config.confluence["api_token"],
        upload=not args.dry_run,
        force=args.force,
    )

    # find all timelog pages
    pages = confluence.find_pages()
    # print('Projects:', projects, sep='\n')
    # print('Pages with title "TimeLog":', pages, sep='\n')

    # process logged time for each project
    json_work_units = dict()
    for i, (page_id, space_name) in enumerate(pages):

        space_name_norm = normalize_project_id(space_name)

        # filter all spaces except the requested on, if one has been requested
        if args.space is not None and args.space != space_name and args.space != space_name_norm:
            print("{:>25s}: ignored due to --space option being set.".format(space_name))

        # find the corresponding redmine project if possible
        elif space_name_norm in projects or space_name_norm.replace("NBIS ", "") in projects:
            try:
                project = projects[space_name_norm]
            except KeyError:
                project = projects[space_name_norm.replace("NBIS ", "")]

            # get logged time for project
            work_units = [ {'date':datetime.strptime(unit['_decoded_attrs']['spent_on'], '%Y-%m-%d'), 'hours':unit['_decoded_attrs']['hours']} for unit in project.time_entries ]
            json_work_units[space_name] = [
                work_unit_to_json(unit) for unit in work_units
            ]
            hours_spent = work_hours(work_units)
            # print('Project:\n', project)

            # get the hour budget
            try:
                budget = float([attr['value'] for attr in project['_decoded_attrs']['custom_fields'] if attr['name'] == 'Hours ordered'][0])
            except:
                # set to zero if the budget for any reason can't be converted to a float (index, key, type errors)
                budget = 0

            print(f"{i}/{len(pages)}\t",
                    end="")
            print(
                    "(redmine id: {})\t{:>25s}: {:7.2f} of {:4.0f} hours: {:6.1%}".format(
                    project['_decoded_attrs']['id'],
                    project['_decoded_attrs']['subject'],
                    hours_spent,
                    budget,
                    hours_spent / budget if budget > 0 else 0.0,
                ),
                end=" ",
            )
            try:
                updated = confluence.update_report_page(
                    space_name, page_id, work_units, budget
                )
                print("(updated)" if updated else "(not updated)")
            except requests.exceptions.HTTPError as e:
                print("\nCould not update project {}: {}".format(space_name, e))
            if work_units and emailer.update(
                project['_decoded_attrs']['subject'], hours_spent, project_start_date=work_units[-1]['date']
            ):
                print("E-Mail sent for", project['_decoded_attrs']['subject'])
        else:
            print("{:>25s}: not found in Redmine".format(space_name.replace("NBIS ", "")))
    db.close()

    if args.dump:
        with open(args.dump, "w") as f:
            print(json.dumps(json_work_units, indent=2), file=f)


if __name__ == "__main__":
    main()
    print("Done.")
