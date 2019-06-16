import json
import os
import re
import requests
import socket
import sys
import time

import feedparser
import htmlslacker

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

GHLINK = {"api.": "", "repos/": "", "users/": "", "pulls": "pull"}
TIMEOUT = 30

# https://developer.github.com/v3/activity/notifications/#list-your-notifications
TEMPLATES = {
    "message": "*<{fix_link(n['subject']['url'])}|{n['subject']['title']}>*\\n_{n['updated_at']}_ - {n['subject']['type']}",

    "events": "_{e['event']}_ by <{e['actor']['html_url']}|@{e['actor']['login']}>",

    "comments": "_<{c['html_url']}|comment>_ by <{c['user']['html_url']}|@{c['user']['login']}>\\n\\n{c['body']}",

    "opened": "_opened_ by <{o['user']['html_url']}|@{o['user']['login']}>\\n\\n{o['body']}",

    "filter": "head_ref_deleted, head_ref_restored, assigned, labeled",

    "feed": "*<{e.link}|{e.title}>* {e.author}\\n\\n{e.summary}"
}

class State(object):
    config = None
    datafile = "noti.dat"
    data = {}
    ghfilter = []
    ini = "config.ini"
    limit = 5000
    logger = None
    sleep = 600
    user = ""

class Log(object):
    def __init__(self, name, mode):
        self.file = open(name, mode)
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        sys.stdout = self
        sys.stderr = self
    def close(self):
        sys.stdout = self.stdout
        sys.stderr = self.stderr
        self.file.close()
    def write(self, data):
        try:
            self.file.write(data)
        except:
            pass
        if self.stdout is not None:
            self.stdout.write(data)
        self.flush()
    def flush(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        if self.stdout is not None:
            self.stdout.flush()

class RateLimit(Exception):
    pass

def get_script_path():
    if getattr(sys, "frozen", False) is False:
        # Script mode
        return os.path.normpath(os.path.join(os.getcwd(), sys.argv[0]))

    # Frozen mode
    return sys.executable

def get_sibling_file(fname):
    return os.path.join(os.path.dirname(get_script_path()), fname)

def start_logger():
    if State.config.getboolean("settings", "log", fallback=False):
        State.logger = Log(get_sibling_file("noti.log"), "w")

def load_data():
    State.datafile = get_sibling_file(State.datafile)
    if os.path.exists(State.datafile):
        with open(State.datafile, "r") as f:
            State.data = json.load(f)

def save_data():
    with open(State.datafile, "w") as f:
        json.dump(State.data, f)

def parse_config():
    State.config = configparser.ConfigParser()
    State.ini = get_sibling_file(State.ini)
    if not os.path.exists(State.ini):
        print("Config not found: " + State.ini)
        sys.exit()

    State.config.read(State.ini)

    State.ghfilter = [
        i.strip() for i in State.config.get(
            "output", "filter", fallback=TEMPLATES["filter"]).split(",")]

    State.sleep = State.config.getint("settings", "sleep", fallback=State.sleep)

    start_logger()
    load_data()

def get_token(service):
    token = State.config.get("auth", service)
    tokenfile = os.path.expanduser(token)
    if os.path.exists(tokenfile):
        token = open(tokenfile).read().strip()
    else:
        env = os.getenv(token)
        if env is not None:
            if os.path.exists(env):
                token = open(env).read()
            else:
                token = env

    return token

def multireplace(string, replacements):
    substrs = sorted(replacements, key=len, reverse=True)

    regexp = re.compile('|'.join(map(re.escape, substrs)))

    return regexp.sub(lambda match: replacements[match.group(0)], string)

def fix_link(link):
    return multireplace(link, GHLINK)

###
# Github

def get_github_auth():
    token = get_token("github")

    return {"Authorization": "token " + token}

def get_github(url, method=requests.get, retry=2):
    if State.limit == 0 and time.time() < State.until:
        print("Rate limit exceeded")
        raise RateLimit()

    if retry == 0:
        return ""

    try:
        print("Opening " + url)
        r = method(url, headers=get_github_auth(), timeout=TIMEOUT)
        time.sleep(0.1)
    except requests.exceptions.ConnectionError:
        time.sleep(30)
        print("Retry connection error: get_github()")
        return get_github(url, method, retry-1)

    if r.status_code < 300:
        State.limit = int(r.headers["X-RateLimit-Remaining"])
        State.until = int(r.headers["X-RateLimit-Reset"])
        if r.text:
            return r.json()
        else:
            return ""
    elif r.status_code == 401:
        print("Unauthorized - check token")
    else:
        print(url + ":", r.status_code)

    return ""

def get_user():
    if not len(State.user):
        State.user = get_github("https://api.github.com/user")["login"]

    return State.user

def get_notifications():
    url = "https://api.github.com/notifications?participating=false"
    if "--all" in sys.argv:
        url += "&all=true"

    return get_github(url)

def mark_read(url):
    get_github(url, requests.patch)

###
# Slack

def post_slack(channel, text, user="", avatar="", retry=2):
    if not State.config.get("auth", "slack", fallback=None):
        print("Slack auth not provided")
        return

    if not text or retry == 0:
        return False

    params = {}
    params["token"] = get_token("slack")
    params["channel"] = "#" + channel
    params["text"] = text
    if user:
        params["as_user"] = "false"
        params["username"] = user
        if avatar:
            params["icon_url"] = avatar
    else:
        params["as_user"] = "true"
    params["unfurl_media"] = "false"
    params["unfurl_links"] = "false"
    url = "https://slack.com/api/chat.postMessage"

    try:
        print("Posting " + url)
        r = requests.post(url, params, timeout=TIMEOUT)
        time.sleep(1)
    except requests.exceptions.ConnectionError:
        time.sleep(30)
        print("Retry connection error: post_slack()")
        return post_slack(channel, text, user, avatar, retry-1)

    if r.text:
        if r.json()["ok"] == False:
            time.sleep(30)
            print("Retry since returned false")
            return post_slack(channel, text, user, avatar, retry-1)
    else:
        time.sleep(30)
        print("Retry since blank reply from Slack")
        return post_slack(channel, text, user, avatar, retry-1)

    return True

def process_github():
    if not State.config.get("auth", "github", fallback=None):
        return

    def emsg(n, e):
        if get_user() == e["actor"]["login"]:
            return ""
        msg = " " + eval('f"%s"' % State.config.get(
            "output", "events", fallback=TEMPLATES["events"]).replace('"', "'"))

        if e["event"] == "labeled":
            l = get_github(n["subject"]["url"].replace("pulls", "issues") + "/labels")
            msg += " " + f"_{l[-1]['name'].title()}_"

        return msg

    def cmsg(n, c):
        if get_user() == c["user"]["login"]:
            return ""
        return " " + eval('f"%s"' % State.config.get(
            "output", "comments", fallback=TEMPLATES["comments"]).replace('"', "'"))

    def omsg(n, o):
        if get_user() == o["user"]["login"]:
            return ""
        return " " + eval('f"%s"' % State.config.get(
            "output", "opened", fallback=TEMPLATES["opened"]).replace('"', "'"))

    notis = get_notifications()
    print(f"Total notifications = {len(notis)}")
    for n in reversed(notis):
        msg = eval('f"%s"' % State.config.get(
            "output", "message", fallback=TEMPLATES["message"]).replace('"', "'"))

        if "commits/" not in n["subject"]["url"] and "releases/" not in n["subject"]["url"]:
            e = get_github(n["subject"]["url"].replace("pulls", "issues") + "/events")
            c = get_github(n["subject"]["url"].replace("pulls", "issues") + "/comments")
            if len(e) and len(c):
                if e[-1]["created_at"] > c[-1]["created_at"]:
                    if e[-1]["event"] in State.ghfilter:
                        msg = ""
                    else:
                        msg += emsg(n, e[-1])
                else:
                    msg += cmsg(n, c[-1])
            elif len(e):
                if e[-1]["event"] in State.ghfilter:
                    msg = ""
                else:
                    msg += emsg(n, e[-1])
            elif len(c):
                msg += cmsg(n, c[-1])
            else:
                o = get_github(n["subject"]["url"].replace("pulls", "issues"))
                if len(o):
                    msg += omsg(n, o)

        once = False
        for section in State.config.sections():
            channel = ""
            match = True
            if "github:" not in section:
                continue

            for key, val in State.config.items(section):
                if key == "channel":
                    channel = val
                else:
                    dictpath = ""
                    for k in key.split("."):
                        dictpath += '["' + k + '"]'
                    nval = eval("n%s" % dictpath)
                    for v in val.split(","):
                        v = v.strip()
                        flip = False
                        if v[0] == "!":
                            v = v[1:]
                            flip = True
                        if (not flip and v not in nval) or (flip and v in nval):
                            match = False
                            break
                    if not match:
                        break

            if match:
                once = True

                if msg:
                    print("----------------")
                    print(channel + " <= " + msg)
                    post_slack(channel, msg,
                        n['repository']['full_name'],
                        n['repository']['owner']['avatar_url'])

        if not once and msg:
            print("----------------")
            print("Dropped <= " + msg)

        mark_read(n["url"])

###
# Feeds

def clean_summary(summary):
	summary = htmlslacker.HTMLSlacker(summary).get_output()
	summary = summary.replace("twitter-atreply pretty-link js-nav|ltr|", "")
	summary = re.sub("<(slashpop|nobg).*?> ", "", summary)
	return summary

def post_entry(channel, e, feed_title):
    if hasattr(e, "summary"):
        e.summary = clean_summary(e.summary)
    else:
        print("RSS has no summary")
        print(e)
        return False
    if not hasattr(e, "author"):
        e.author = ""
    else:
        e.author = "by " + e.author
    e.title = e.title.split("\n")[0]
    msg = eval('f"%s"' % State.config.get("output", "feed",
        fallback=TEMPLATES["feed"]).replace('"', "'"))
    print("----------------")
    print(channel + " <= " + msg)
    post_slack(channel, msg, feed_title)

    return True

def process_feeds():
    for section in State.config.sections():
        if "feed:" not in section:
            continue

        source = State.config.get(section, "source", fallback="")
        channel = State.config.get(section, "channel", fallback="")
        method = State.config.get(section, "method", fallback="date")

        if not source or not channel:
            print("Source or channel not specified for " + section)
            continue

        if method not in ["diff", "date"]:
            print("Bad method specified - diff|date supported")
            continue

        feed = feedparser.parse(source)
        if method == "date":
            last = 0.0
            if section in State.data and isinstance(State.data[section], float):
                last = State.data[section]
            else:
                State.data[section] = 0.0
            for e in sorted(feed.entries,
                key=lambda entry: time.mktime(entry.updated_parsed)):
                    curr = time.mktime(e.updated_parsed)
                    if curr > last and post_entry(channel, e, feed.feed.title):
                        last = curr
            if State.data[section] < last:
                State.data[section] = last
                save_data()
        elif method == "diff":
            if section not in State.data or isinstance(State.data[section], float):
                State.data[section] = []
            for e in feed.entries:
                if e.link not in State.data[section]:
                    post_entry(channel, e, feed.feed.title)
                else:
                    State.data[section].remove(e.link)
                State.data[section].append(e.link)
            if len(State.data[section]) > 500:
                State.data[section].pop(0)
            save_data()

def main():
    socket.setdefaulttimeout(TIMEOUT)
    parse_config()
    try:
        while True:
            print("Processing queue")
            try:
                process_github()
                process_feeds()
            except RateLimit:
                pass
            print("Done processing")
            time.sleep(State.sleep)
    except (KeyboardInterrupt, ConnectionError, SystemExit):
        pass

if __name__ == "__main__":
    main()
