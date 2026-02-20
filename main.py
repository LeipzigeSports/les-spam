import email
import imaplib
import json
import logging.config
import os
import re
import sqlite3
from email import policy
from pathlib import Path

from dotenv import load_dotenv

# set up global config and logging
root_dir = Path(__file__).parent
db_path = root_dir / "data" / "spam-reports.db"

with (root_dir / "logging.json").open(mode="r") as f:
    logging.config.dictConfig(json.load(f))

logger = logging.getLogger(__name__)


def init_db():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS spam_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eml_subject TEXT,
            eml_date TEXT,
            eml_filename TEXT,
            spam_score REAL,
            spam_score_required REAL,
            parent_subject TEXT,
            parent_from TEXT,
            parent_date TEXT
        )
    """)
    conn.commit()
    return conn


def extract_spam_score(eml_msg: email.message.Message):
    status = eml_msg.get("X-Spam-Status")

    if not status:
        return None

    match_score = re.search(r"score=([-\d.]+)", status, re.IGNORECASE)
    match_required = re.search(r"required=([-\d.]+)", status, re.IGNORECASE)

    if not match_score or not match_required:
        return None

    return float(match_score.group(1)), float(match_required.group(1))


def process_mailbox(
    imap_server: str, imap_port: int, imap_username: str, imap_password: str
):
    global logger
    logger = logging.getLogger(__name__)

    db_conn = init_db()
    db_cursor = db_conn.cursor()

    mail = imaplib.IMAP4_SSL(imap_server, imap_port)

    try:
        # connect
        mail.login(imap_username, imap_password)
        # open inbox
        mail.select("INBOX")

        # select unseen messages
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            raise IOError("Failed to read mails")

        # email ids are separated by spaces
        email_ids = messages[0].split()
        logger.info(f"Found {len(email_ids)} unread mails")

        # process mails
        for email_id in email_ids:
            # PEEK ensures that mail is not marked as read
            status, email_data = mail.fetch(email_id, "(BODY.PEEK[])")
            if status != "OK":
                logger.warning(f"Failed to read body of mail with ID {email_id}")
                continue

            # email_data[0] contains the email data
            # email_data[0][0] contains body directive
            # email_data[0][1] contains actual email source
            raw_email_data = email_data[0][1]

            # parse email source contents
            msg = email.message_from_bytes(raw_email_data, policy=policy.default)

            parent_subject = msg.get("Subject", None)
            parent_from = msg.get("From", None)
            parent_date = msg.get("Date", None)

            # check if mail is multipart
            if msg.is_multipart():
                # iterate over parts
                for part in msg.walk():
                    content_type = part.get_content_type()
                    eml_filename = part.get_filename() or ""

                    # skip parts that aren't eml files
                    if (
                        content_type != "message/rfc822"
                        or not eml_filename.lower().endswith(".eml")
                    ):
                        continue

                    # decode attachment
                    eml_payload = part.get_payload(decode=True)

                    # eml may be embedded as multipart/mixed
                    if not eml_payload:
                        # if part is not a list, continue
                        if not isinstance(part.get_payload(), list):
                            continue

                        # otherwise get embedded payload
                        eml_payload = bytes(part.get_payload(0))

                    # parse eml
                    eml_msg = email.message_from_bytes(
                        eml_payload, policy=policy.default
                    )

                    eml_subject = eml_msg.get("Subject", None)
                    eml_date = eml_msg.get("Date", None)

                    eml_score = extract_spam_score(eml_msg)
                    if not eml_score:
                        logger.warning(
                            f'eml "{eml_filename}" sent by "{parent_from}" does not contain spam assassin headers'
                        )
                        continue

                    # store in sqlite db
                    db_cursor.execute(
                        """
                        INSERT INTO spam_reports (eml_subject, eml_date, eml_filename, spam_score, spam_score_required, parent_subject, parent_from, parent_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            eml_subject,
                            eml_date,
                            eml_filename,
                            eml_score[0],
                            eml_score[1],
                            parent_subject,
                            parent_from,
                            parent_date,
                        ),
                    )

                    db_conn.commit()

            # mark mail as seen
            mail.store(email_id, "+FLAGS", "\\Seen")

    except Exception:
        logger.exception("Error during processing", exc_info=True)
    finally:
        db_conn.close()

        try:
            mail.close()
            mail.logout()
        except Exception:
            logger.exception("Failed to close connection", exc_info=True)


if __name__ == "__main__":
    load_dotenv()

    imap_server = os.getenv("IMAP_SERVER")
    imap_port = int(os.getenv("IMAP_PORT", 993))
    imap_username = os.getenv("IMAP_USERNAME")
    imap_password = os.getenv("IMAP_PASSWORD")

    if not all([imap_server, imap_username, imap_password]):
        logger.error("Missing configuration parameters")
        exit(1)

    try:
        process_mailbox(imap_server, imap_port, imap_username, imap_password)
    except Exception:
        logger.exception("Unhandled error in main function", exc_info=True)
        exit(1)
