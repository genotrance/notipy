notipy is a Python script that pushes Github notifications and RSS feeds to Slack

notipy exists since the Github app on Slack only monitors your own repos and
needs you to subscribe to them one at a time. Further, it does not allow you to
monitor notifications which can originate from other watched repos.

It also includes support for RSS feeds since the Slack native method is very
verbose in its output and hard to read.

# Installation

`> git clone https://github.com/genotrance/notipy`

Edit `config.ini` as required

`> pip install feedparser htmlslacker`
`> python noti.py` 

# Feedback

notipy is a work in progress and any feedback or suggestions are welcome. It is
hosted on GitHub with an MIT license so issues, forks and PRs are most appreciated.
