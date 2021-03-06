import datetime
import email
import logging
import os
import re
import sys
import time
from email.encoders import encode_base64
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from imaplib import ParseFlags
from mimetypes import guess_type

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()

if sys.version_info[0] == 2:
    unicode_type = unicode
else:
    unicode_type = str


class Message:
    sent_at = None

    def __init__(self,
                 mailbox,
                 uid):

        self.uid = uid
        self.mailbox = mailbox
        self.gmail = mailbox.gmail if mailbox else None

        # this is the wrapped object. based on the type this can be a MimeText
        # or MimeMultipart
        self.message = None
        self.headers = {}

        self.subject = None
        self.body = None
        self.html = None

        self.to = None
        self.fr = None
        self.cc = None
        self.delivered_to = None

        self.sent_at = None

        self.flags = []
        self.labels = []

        self.thread_id = None
        self.thread = []
        self.message_id = None

        self.attachments = None

    def __repr__(self):
        return f'<Message {self.uid}>'

    def __getattribute__(self, item):
        if not object.__getattribute__(self, item):
            self._fetch()
        return object.__getattribute__(self, item)

    @staticmethod
    def create(subject,
               to,
               cc=None,
               bcc=None,
               text=None,
               is_html=False,
               attachments=None,
               sender=None,
               reply_to=None):
        """
        returns: MIMEMultipart or MIMEText. Currently as a SMTP message doesnt require any of the methods which
                 are provided by this message class, this create method doesnt return a Message object.
        """
        if not is_html and attachments is None:
            # Simple plain text email
            message = MIMEText(text, 'plain', charset(text))
        else:
            # Multipart message
            message = MIMEMultipart()
            if is_html:
                # Add html & plain text alernative parts
                alt = MIMEMultipart('alternative')
                alt.attach(MIMEText(text, 'html', charset(text)))
                # alt.attach(MIMEText(html,'html',charset(html)))
                message.attach(alt)
            else:
                # Just add plain text part
                txt = MIMEText(text, 'plain', charset(text))
                message.attach(txt)
            # Add attachments
            for a in attachments or []:
                message.attach(Message._file_to_mime_attachment(a))
        # Set headers
        message['To'] = to
        if cc:
            message['Cc'] = cc
        if bcc:
            message['Bcc'] = bcc

        if sender:
            message['From'] = sender

            if not reply_to:
                # If 'Reply-To' is not provided, set it to the 'From' value
                message['Reply-To'] = sender

        if message['Date'] is None:
            message['Date'] = formatdate(time.time(), localtime=True)
        if message['Message-ID'] is None:
            message['Message-ID'] = make_msgid()

        if reply_to:
            message['Reply-To'] = reply_to

        message['Subject'] = subject

        return message

    @property
    def from_(self):
        if '<' in self.fr and '>' in self.fr:
            return self.fr.split('<')[-1].replace('>', '')
        return self.fr

    @property
    def is_read(self):
        return '\\Seen' in self.flags

    def read(self):
        flag = '\\Seen'
        self.gmail.imap.uid('STORE', self.uid, '+FLAGS', flag)
        if flag not in self.flags:
            self.flags.append(flag)

    def unread(self):
        flag = '\\Seen'
        self.gmail.imap.uid('STORE', self.uid, '-FLAGS', flag)
        if flag in self.flags:
            self.flags.remove(flag)

    @property
    def is_starred(self):
        return '\\Flagged' in self.flags

    def star(self):
        flag = '\\Flagged'
        self.gmail.imap.uid('STORE', self.uid, '+FLAGS', flag)
        if flag not in self.flags:
            self.flags.append(flag)

    def unstar(self):
        flag = '\\Flagged'
        self.gmail.imap.uid('STORE', self.uid, '-FLAGS', flag)
        if flag in self.flags:
            self.flags.remove(flag)

    @property
    def is_draft(self):
        return '\\Draft' in self.flags

    def has_label(self, label):
        full_label = '%s' % label
        return full_label in self.labels

    def add_label(self, label):
        full_label = '%s' % label
        self.gmail.imap.uid('STORE', self.uid, '+X-GM-LABELS', full_label)
        if full_label not in self.labels:
            self.labels.append(full_label)

    def remove_label(self, label):
        full_label = '%s' % label
        self.gmail.imap.uid('STORE', self.uid, '-X-GM-LABELS', full_label)
        if full_label in self.labels:
            self.labels.remove(full_label)

    @property
    def is_deleted(self):
        return '\\Deleted' in self.flags

    def delete(self):
        flag = '\\Deleted'
        self.gmail.imap.uid('STORE', self.uid, '+FLAGS', flag)
        if flag not in self.flags:
            self.flags.append(flag)

        trash = '[Gmail]/Trash' if '[Gmail]/Trash' in self.gmail.labels() else '[Gmail]/Bin'
        if self.mailbox.name not in ['[Gmail]/Bin', '[Gmail]/Trash']:
            self.move_to(trash)

    def move_to(self, name):
        self.gmail.copy(self.uid, name, self.mailbox.name)
        if name not in ['[Gmail]/Bin', '[Gmail]/Trash']:
            self.delete()

    def archive(self):
        self.move_to('[Gmail]/All Mail')

    def parse(self, raw_message):
        raw_headers = raw_message[0].decode()
        raw_email = raw_message[1]

        self.message = email.message_from_bytes(raw_email)
        self.headers = parse_headers(self.message)

        self.to = self.message['to']
        self.fr = self.message['from']
        self.delivered_to = self.message['delivered_to']

        self.subject = parse_subject(self.message['subject'])

        if self.message.get_content_maintype() == "multipart":
            for content in self.message.walk():
                if content.get_content_type() == "text/plain":
                    self.body = content.get_payload(decode=True)
                elif content.get_content_type() == "text/html":
                    self.html = content.get_payload(decode=True)
        elif self.message.get_content_maintype() == "text":
            self.body = self.message.get_payload()

        self.sent_at = datetime.datetime.fromtimestamp(
            time.mktime(email.utils.parsedate_tz(self.message['date'])[:9]))

        self.flags = parse_flags(raw_headers)

        self.labels = parse_labels(raw_headers)

        if re.search(r'X-GM-THRID (\d+)', raw_headers):
            self.thread_id = re.search(
                r'X-GM-THRID (\d+)', raw_headers).groups(1)[0]
        if re.search(r'X-GM-MSGID (\d+)', raw_headers):
            self.message_id = re.search(
                r'X-GM-MSGID (\d+)', raw_headers).groups(1)[0]

        # Parse attachments into attachment objects array for this message
        self.attachments = [a for a in [

            Attachment(attachment)
            for attachment in self.message._payload
            if not isinstance(attachment, str)
            if attachment.get('Content-Disposition') is not None

        ] if a]

    def _fetch(self):
        _, results = self.gmail.imap.uid(
            'FETCH', self.uid, '(BODY.PEEK[] FLAGS X-GM-THRID X-GM-MSGID X-GM-LABELS)')

        self.parse(results[0])

        return self.message

    @property
    def string_sent_at(self):
        return self.sent_at.strftime('%-m/%-d/%y')

    @staticmethod
    def _file_to_mime_attachment(file):
        """
            Create MIME attachment
        """
        if isinstance(file, MIMEBase):
            # Already MIME object - return
            return file
        else:
            # Assume filename - guess mime-type from extension and return MIME
            # object
            main, sub = (guess_type(file)[
                             0] or 'application/octet-stream').split('/', 1)
            attachment = MIMEBase(main, sub)
            with open(file, 'rb') as f:
                attachment.set_payload(f.read())
            attachment.add_header('Content-Disposition',
                                  'attachment', filename=os.path.basename(a))
            encode_base64(attachment)
            return attachment


Message.mark_as_read = Message.read
Message.mark_as_unread = Message.unread
Message.date = Message.sent_at
Message.string_date = Message.string_sent_at


class Attachment:

    def __init__(self, attachment):
        self.name = attachment.get_filename()
        # Raw file data
        self.payload = attachment.get_payload(decode=True)
        # Filesize in kilobytes
        try:
            self.size = int(round(len(self.payload) / 1000.0))
        except TypeError:
            self.size = 0

    def __bool__(self):
        return bool(self.size)

    def __repr__(self):
        return f'<Attachment {self.name}'

    def save(self, path=None):
        if path is None:
            # Save as name of attachment if there is no path specified
            path = self.name
        elif os.path.isdir(path):
            # If the path is a directory, save as name of attachment in that
            # directory
            path = os.path.join(path, self.name)

        with open(path, 'wb') as f:
            f.write(self.payload)


def charset(s):
    return 'utf-8' if isinstance(s, unicode_type) else 'us-ascii'


def parse_headers(message):
    hdrs = {}
    for hdr in list(message.keys()):
        hdrs[hdr] = message[hdr]
    return hdrs


def parse_flags(headers):
    if sys.version_info[0] == 3:
        headers = bytes(headers, "ascii")
    return list(ParseFlags(headers))


def parse_labels(headers):
    if re.search(r'X-GM-LABELS \(([^\)]+)\)', headers):
        labels = re.search(
            r'X-GM-LABELS \(([^\)]+)\)', headers).groups(1)[0].split(' ')
        return [l.replace('"', '') for l in labels]
    else:
        return list()


def parse_subject(encoded_subject):
    dh = decode_header(encoded_subject)
    return ''.join([str(t[0]) for t in dh])

