# The NBIS Long-Term Support TimeLog bot

To start using this bot, you need to have a “space” for a long-term support
project on the [SciLifeLab Confluence](https://scilifelab.atlassian.net/).
Within the space, create a page named “TimeLog”. The page will be updated the
next time the bot runs. Currently, runs are scheduled at 12:00 and 23:00 daily.

You can add your own custom text to the TimeLog page if you write it just below
the title. Any text before the first horizontal line (horizontal rule,
&lt;hr&gt; tag in HTML) is kept by the bot.

The bot retrieves time logging information from
[TimeLog](https://app3.timelog.com/scilifelab/). For this to work, the space
name in the wiki and the project name in TimeLog must be the same.


## Other information

- The bot runs as a crontab job on the NBIS server Henry or Hertz under the account
  **TBD**.
- The bot sends e-mail notifications to the three managers when one of the
  following events occur:
    * A project has reached 365 days since it was created
    * A project has for the first time reached 100 or 300 logged hours
- On Confluence, the bot makes its changes under the user id “timelogbot@nbis.se”
- For TimeLog, it uses an API key
