# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

if False:
    import logging
    logging.basicConfig(level=1)

import base64
import email
import email.policy
import imaplib
import logging
import pywikibot
import re
import urllib
import urllib3

from pywikibot.bot import CurrentPageBot, SingleSiteBot

logger = logging.getLogger(__name__)

class IMAP4JobQueue(imaplib.IMAP4_SSL):
    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        password = kwargs.pop("password", "")
        self.mailbox = kwargs.pop("mailbox", "INBOX")
        logger.debug("connecting to IMAP server")
        super().__init__(*args, **kwargs)
        if user is not None:
            logger.debug("authenticating")
            self._chk(self.login(user, password))
        logger.info(f"connected to {self.host}")

    def _chk(self, typ_dat):
        typ, dat = typ_dat
        if typ != "OK":
            raise self.error(dat[-1].decode(errors="ignore"))
        return dat

    @property
    def _messages(self):
        logger.debug(f"selecting {self.mailbox}")
        data = self._chk(self.select(self.mailbox))
        if data and data[0] == b"0":
            return
        logger.debug("fetching messages")
        for data in self._chk(self.fetch("1:*", "(UID RFC822)")):
            # Each data is either a string, or a tuple.
            if not isinstance(data, tuple):
                assert data == b")" # XXX why?
                continue
            # If a tuple, then the first part is the header
            # of the response, and the second part contains
            # the data (ie: 'literal' value).
            header, data = data
            header = header.split()
            assert header[1] == b"(UID"
            assert header[-2] == b"RFC822"
            assert header[-1] == b"{%d}" % len(data)
            imap_uid = header[2]
            yield imap_uid, data

    @property
    def messages(self):
        for imap_uid, data in self._messages:
            logger.debug(f"handling UID {imap_uid}")
            msg = email_from_bytes(data)
            for header in ("to", "cc", "bcc"):
                if msg[header]:
                    continue
            encoded_bytes = base64.a85encode(data, wrapcol=72)
            pywikibot.output(f"{encoded_bytes.decode('ascii')}\n")
            assert not hasattr(msg, "uid")
            msg.uid = imap_uid
            yield msg

def email_from_bytes(data):
    return email.message_from_bytes(
        data, policy=email.policy.default)

class Robot(SingleSiteBot, CurrentPageBot):
    def __init__(self, **kwargs):
        super(Robot, self).__init__(site=True, **kwargs)
        self._mbox_args = self.site.family.readinglist.mailbox
        self._mbox = None

    @property
    def mbox(self):
        if self._mbox is None:
            self._mbox = IMAP4JobQueue(**self._mbox_args)
        return self._mbox

    @property
    def generator(self):
        self.entries = []
        for msg in self.mbox.messages:
            entry = self.entry_for(msg)
            if entry is None:
                continue
            self.entries.append(entry)
            self.mbox._chk(self.mbox.uid("STORE", msg.uid,
                                         "+FLAGS", r"\Deleted"))
        if self.entries:
            yield pywikibot.Page(self.site, "Reading list")

    def treat_page(self):
        # Add the new entries.
        bits = [self.current_page.text.rstrip()]
        bits.extend(self.entries)
        text = "\n* ".join(bits) + "\n"
        # Strip duplicate entries.
        lines, seen = [], {}
        for line in text.rstrip().split("\n"):
            m = re.match(r"\*\s*(\{\{at\|.*?\}\}\s*)?", line)
            if m is not None:
                entry = line[len(m.group(0)):]
                if entry in seen:
                    continue
                seen[entry] = True
                line
            lines.append(line)
        # Store the updated wikitext.
        self.put_current("\n".join(lines) + "\n",
                         show_diff=(not self.getOption("always")),
                         asynchronous=False)
        self.mbox._chk(self.mbox.expunge())

    REWRITES = (
        (r"^https?://en\.(m\.)?wikipedia\.org/wiki/", "wikipedia:"),
        (r"^https?://youtu\.be/", "https://www.youtube.com/watch?v="),
        (r"\?igshid=[a-z0-9+/]*={0,2}", ""),
    )

    @classmethod
    def entry_for(cls, msg):
        body = msg.get_body(('plain',))
        if body is None:
            return
        body = body.get_content().strip()
        if not body:
            return
        body = body.split(maxsplit=1)
        if len(body) == 1:
            body.append(None)
        entry, body = body
        if not entry:
            return
        if entry.startswith("<") and entry.endswith(">"):
            entry = entry[1:-1]
        subject = msg["subject"]
        if subject is not None:
            subject = subject.strip()
        if subject:
            if entry.split(":", 1)[0].lower() not in ("http", "https"):
                return
        for pattern, repl in cls.REWRITES:
            entry = re.sub(pattern, repl, entry, 1, re.I)
        if entry.startswith("wikipedia:"):
            entry = urllib.parse.unquote(entry).replace("_", " ")
            entry = "[[%s]]" % entry
            if subject:
                entry = "%s ''<q>%s</q>''" % (entry, subject)
        elif subject:
            entry = "%s %s" % (entry, subject)
            if "[" not in entry and "]" not in entry:
                entry = "[%s]" % entry
        date = msg["date"]
        if date is not None:
            entry = "{{at|%s}} %s" % (date, entry)
        if body is not None:
            entry = "%s %s" % (entry, body)
        return entry

class LogScrobbler(logging.Filterer):
    def __init__(self, logger):
        super(LogScrobbler, self).__init__()
        self.logger = logger

    def __enter__(self):
        self.records = []
        self.logger.addFilter(self)

    def filter(self, record):
        """Return True if the record should be logged, False otherwise."""
        self.records.append(record)
        return False

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.logger.removeFilter(self)
        # If it looks like we succeeded then lose network errors
        if exc_type is None and self.success_message_seen:
            self.purge_network_errors()
        # Feed the remaining records back into the system
        for record in self.records:
            self.logger.handle(record)

    SUCCESS_MESSAGES = (
        "Page [[Reading list]] saved",
        "No changes were needed on [[Reading list]]",
    )

    @property
    def success_message_seen(self):
        for record in reversed(self.records):
            if record.msg in self.SUCCESS_MESSAGES:
                return True
        return False

    def purge_network_errors(self):
        self.records = [record
                        for record in self.records
                        if not self.is_network_error(record)]

    @classmethod
    def is_network_error(cls, record):
        if record.levelname != "ERROR":
            return False
        if record.msg.startswith("An error occurred for uri "):
            return True
        if not record.exc_info:
            return False
        if isinstance(record.exc_info[1], urllib3.exceptions.HTTPError):
            return True
        pywikibot.output(f"{record.exc_info!r}")
        return False # XXX

def _main(*args):
    args = pywikibot.handle_args(args)
    assert not args
    try:
        bot = Robot()
        bot.site.login()
        if False:  # XXX sys.stdin.isatty()
            bot.run()
        else:
            bot.options["always"] = True
            for page in bot.generator:
                bot._current_page = page
                bot.treat(page)
    except IMAP4JobQueue.error as e:
        if not e.args:
            raise
        msg = e.args[0].lower()
        for needle in ("deleted under",
                       "please relogin"):
            if msg.find(needle) >= 0:
                break
        else:
            raise

def main(*args):
    with LogScrobbler(logging.getLogger("pywiki")):
        return _main(*args)

if __name__ == "__main__":
    if "sys" not in locals():
        import sys
    assert sys.version_info >= (3,)
    main()
