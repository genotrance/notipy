import json
import os
import re
import requests
import sys
import time

import feedparser
import htmlslacker

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

GHLINK = {"api.": "", "repos/": "", "users/": "", "pulls": "pull"}

# https://developer.github.com/v3/activity/notifications/#list-your-notifications
TEMPLATES = {
    "message": "*<{fix_link(n['subject']['url'])}|{n['subject']['title']}>*\\n_{n['updated_at']}_ - {n['subject']['type']}",

    "events": "_{e['event']}_ by <{e['actor']['html_url']}|@{e['actor']['login']}>",

    "comments": "_<{c['html_url']}|comment>_ by <{c['user']['html_url']}|@{c['user']['login']}>\\n\\n{c['body']}",

    "opened": "_opened_ by <{o['user']['html_url']}|@{o['user']['login']}>\\n\\n{o['body']}",

    "filter": "head_ref_deleted, head_ref_restored, assigned, labeled",

    "feed": "*<{e.link}|{e.title}>* @ _n{e.updated}_ {e.author}\\n\\n{e.summary}"
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

def get_github(url, method=requests.get):
    if State.limit == 0 and time.time() < State.until:
        print("Rate limit exceeded")
        raise RateLimit()

    r = method(url, headers=get_github_auth())
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

    sys.exit()

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
        r = requests.post(url, params)
    except requests.exceptions.ConnectionError:
        time.sleep(5)
        print("Retry connection error")
        return post_slack(channel, text, user, avatar, retry-1)

    if r.text:
        if r.json()["ok"] == False:
            time.sleep(5)
            print("Retry since returned false")
            return post_slack(channel, text, user, avatar, retry-1)
    else:
        time.sleep(5)
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

        if "commits/" not in n["subject"]["url"]:
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

def process_feeds():
    for section in State.config.sections():
        if "feed:" not in section:
            continue

        source = State.config.get(section, "source", fallback="")
        channel = State.config.get(section, "channel", fallback="")

        if not source or not channel:
            print("Source or channel not specified for " + section)
            continue

        feed = feedparser.parse(source)
        last = 0
        if section in State.data:
            last = State.data[section]
        else:
            State.data[section] = 0
        for e in sorted(feed.entries,
            key=lambda entry: time.mktime(entry.updated_parsed)):
                curr = time.mktime(e.updated_parsed)
                if curr > last:
                    e.summary = htmlslacker.HTMLSlacker(
                        e.summary).get_output().replace("twitter-atreply pretty-link js-nav|ltr|", "")
                    if not hasattr(e, "author"):
                        e.author = ""
                    else:
                        e.author = "by " + e.author
                    e.title = e.title.split("\n")[0]
                    msg = eval('f"%s"' % State.config.get("output", "feed",
                        fallback=TEMPLATES["feed"]).replace('"', "'"))
                    print("----------------")
                    print(channel + " <= " + msg)
                    post_slack(channel, msg, feed.feed.title)
                    last = curr
        if State.data[section] < last:
            State.data[section] = last
            save_data()

def main():
    parse_config()
    try:
        while True:
            try:
                process_github()
                process_feeds()
            except RateLimit:
                pass
            time.sleep(State.sleep)
    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass

if __name__ == "__main__":
    main()
