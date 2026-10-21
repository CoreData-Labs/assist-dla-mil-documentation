# ASSIST (assist.dla.mil) PDF downloader - never-stop version: no arguments, every error is logged and skipped.
# Everything (pages, login checks and PDF downloads) goes through Chrome; Chrome logs in with the password it has saved.
# Every ID is checked again on every run (new PDFs can appear later); files that are already saved are never requested again.
# Any Chrome problem (crash, closed window, logout, redirect loop, broken results page) closes Chrome, waits for the internet,
# restarts Chrome, logs in again and resumes at the saved results page.
# Layout of this file: constants, data models, errors, pure helpers, Database, Pacer, NetworkMonitor, Keyboard,
# BrowserSession, ResultsNavigator, DocumentDownloader, Supervisor, main. All run-time state lives in objects created in main().
# Requires Python 3.12 or newer.

import errno  # Recognise the "disk is full" operating system error
import hashlib  # Fingerprint every saved PDF
import json  # Chrome settings file and keep-alive script quoting
import logging  # Console logging
import os  # Operating system name and path helpers
import queue  # Pass typed lines from the keyboard thread to the main thread
import random  # Jitter for the pauses between requests
import re  # Text pattern matching
import shutil  # Measure the free disk space and find the ping command
import sqlite3  # Local database of everything seen and saved in earlier runs
import subprocess  # Run the background ping
import sys  # Standard input and output
import threading  # Keyboard reader thread, ping reader threads and the watchdog thread
import time  # Pauses and timing
from collections.abc import (
    Callable,
    Iterator,
)  # Type hints for callbacks and generators
from dataclasses import dataclass, field  # Data holders
from datetime import datetime, timedelta  # Date formatting and time differences
from email.utils import parsedate_to_datetime  # Parse HTTP dates (Retry-After header)
from pathlib import Path  # Cross-platform file paths
from typing import Any  # Type hints
from urllib.parse import parse_qs, urljoin, urlparse  # URL parsing

try:  # psutil is optional (it is needed to stop Chrome when the internet goes down)
    import psutil  # Find and stop the Chrome processes of this script
except ImportError:  # psutil is not installed
    psutil = None  # The network watchdog then cannot stop Chrome by itself
from lxml import etree  # Catch lxml parsing errors precisely
from lxml import html as lxml_html  # Fast HTML parsing
from playwright.sync_api import (
    BrowserContext,
    Dialog,
    Download,
    Locator,
    Page,
    Playwright,
    Response,
)  # Playwright types
from playwright.sync_api import (
    Error as PlaywrightError,
)  # Network errors, timeouts, closed browser
from playwright.sync_api import sync_playwright  # Controls the Chromium browser

# ===========================================================================
# CONSTANTS (never changed while the script runs)
# ===========================================================================
# --- Timing and retries ---
SECONDS_BETWEEN_REQUESTS = 0.5  # Normal wait in seconds between web requests
MAXIMUM_DELAY_SECONDS = 10  # The adaptive delay never grows beyond this
JITTER_MINIMUM_FACTOR = (
    0.7  # Every pause is multiplied by a random factor of at least this
)
JITTER_MAXIMUM_FACTOR = 1.3  # ... and at most this
REQUEST_TIMEOUT_MS = 120_000  # Network timeout in milliseconds
RETRY_ATTEMPTS = 3  # Number of times to retry a failed operation
SECONDS_PER_RETRY_STEP = 2  # Base backoff time in seconds for retries
MAXIMUM_RETRY_AFTER_SECONDS = (
    300  # Never wait longer than 5 minutes because a server asked us to slow down
)
MAXIMUM_BACKOFF_SECONDS = (
    300  # Waits between paging attempts and breaker pauses never grow beyond 5 minutes
)
# --- Restarts and circuit breaker ---
RESTART_WAIT_SECONDS = 10  # Base wait before the browser session restarts
MAXIMUM_RESTART_WAIT_SECONDS = 300  # The restart wait never grows beyond 5 minutes
MAXIMUM_PAGING_RESTARTS = 5  # Paging failures in a row (without reaching a new results page) that restart Chrome before the run stops
CIRCUIT_BREAKER_FAILURES = 5  # This many failed documents in a row pauses the run
CIRCUIT_BREAKER_LOGIN_CHECK_AT = 2  # After this many failed documents in a row the script checks whether Chrome was logged out
CIRCUIT_BREAKER_BASE_SECONDS = (
    30  # First breaker pause (doubles with every trip in a row)
)
SITE_DOWN_RESTART_AFTER_CHECKS = 3  # After this many failed site checks in a row Chrome is closed and the script waits for the internet
FATAL_BROWSER_ERROR_MARKERS = (
    "has been closed",
    "target closed",
    "crashed",
    "browser closed",
    "connection closed",
    "disconnected",
)  # Lower-case Playwright error texts that mean Chrome is unusable
REDIRECT_LOOP_MARKER = "err_too_many_redirects"  # Lower-case Chrome error text of a redirect loop (the session cookies are invalid)
# --- Network watchdog ---
PING_TARGETS = (
    "1.1.1.1",
    "8.8.8.8",
)  # Addresses to ping: the first on its own, the others (backup) only while the first gets no reply
PING_REPLY_PATTERN = re.compile(
    pattern=r"ttl=", flags=re.IGNORECASE
)  # A ping reply line contains "ttl=" (Linux, macOS and Windows)
PING_BACKUP_AFTER_SECONDS = 3  # When the first address has not replied for this long, the backup address is pinged too
PING_SILENCE_SECONDS = 60  # No ping reply for this long means the internet is down (Chrome is then stopped)
PING_RETRY_WAIT_SECONDS = (
    60  # When ping gets no reply, wait this long and then restart ping
)
PING_FIRST_REPLY_WAIT_SECONDS = (
    10  # After ping was (re)started, wait this long for the first reply
)
PING_CHECK_SECONDS = 2  # How often the background watchdog looks at the ping replies
PING_RESTART_MIN_SECONDS = (
    5  # A ping process that exited is restarted at most this often
)
# --- Disk ---
MINIMUM_FREE_DISK_BYTES = (
    500 * 1024 * 1024
)  # Pause while less than 500 MB of disk space is free
DISK_WAIT_SECONDS = 30  # Wait between free-disk-space checks while the disk is too full
BYTES_PER_MEGABYTE = 1024 * 1024  # Number of bytes in one megabyte
# --- Browser ---
SHOW_BROWSER_WINDOW = True  # True makes the browser visible to the user
BROWSER_LAUNCH_ARGUMENTS = [
    "--start-maximized",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--noerrdialogs",
]  # Chrome opens maximized and never shows the "Restore pages?" bubble or error dialogs
BROWSER_IGNORED_DEFAULT_ARGUMENTS = [
    "--enable-automation",
    "--password-store=basic",
    "--use-mock-keychain",
]  # Playwright flags that switch off Chrome's password manager, so saved passwords can be autofilled
KEEP_ALIVE_SECONDS = 240  # The browser requests a small page this often, so the session does not time out
AUTO_LOGIN_WAIT_SECONDS = (
    15  # How long to wait for Chrome to autofill the saved username and password
)
AUTO_STEP_TIMEOUT_MS = (
    30_000  # Timeout in milliseconds for each automatic click or wait
)
AUTO_OPTIONAL_CLICK_TIMEOUT_MS = (
    5_000  # Timeout for the optional "Search Method" and "Advanced Search" clicks
)
JUMP_TIMEOUT_MS = 30_000  # How long a jump to a typed page number may take before Next-button clicking is used instead
KEYBOARD_POLL_MS = 300  # Polling interval for keyboard input in milliseconds
CLOSED_INPUT_WAIT_SECONDS = (
    5  # If terminal input is closed, prompts continue by themselves after this long
)
# --- Scraping behaviour ---
RESUME_FROM_SAVED_PAGE = True  # True: after a restart, continue at the results page saved in the database (False: always start at page 1)
RESUME_KEY = "results_page"  # Name of the row in the resume table
PAGES_BETWEEN_SKIP_LOGS = (
    10  # While paging forward with the Next button, log every this many pages
)
PAGING_MAXIMUM_ROUNDS = (
    5  # Rounds of attempts to page to or read a results page before Chrome is restarted
)
RETRY_FAILED_EVERY_N_PAGES = (
    5  # Failed documents are retried after every this many results pages
)
MAXIMUM_DOCUMENT_ATTEMPTS = 3  # Failed documents are retried during the run until they failed this often (the final round tries all)
FINAL_RETRY_ROUNDS = 1  # Extra rounds spent on documents that failed
REFUSED_RECHECK_HOURS = (
    24  # A file the server refused is not requested again for this many hours
)
PROGRESS_EVERY_N_DOCUMENTS = 25  # Frequency of progress totals
MAXIMUM_FAILURES_SHOWN = 100  # How many problems the final summary lists
# --- Logging ---
LOG_LINE_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"  # Layout of a log line
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # Layout of the time in a log line

# ===========================================================================
# CONSTANTS: MESSAGES
# ===========================================================================
START_INSTRUCTIONS = (  # Shown only when the automatic log-in fails
    "\n>> In the browser window:\n"  # Header
    "   1. Log in\n"  # Step 1
    "   2. Go to the search page and select Advanced Search\n"  # Step 2
    "   3. Click Search (leave the filters empty to get EVERYTHING)\n"  # Step 3
    "   4. Stay on results page 1 (every ID is checked again on every run, so new IDs and new files are found)\n"  # Step 4
    "   Then press Enter here..."  # Prompt
)  # End of the start instructions
NOT_LOGGED_IN_REMINDER = "\n   You do not seem to be logged in yet. Log in, then press Enter here..."  # Shown when no tab shows a logout link
NO_RESULTS_REMINDER = (  # Shown when the results grid is missing
    "\n   I cannot see the results grid. Select Advanced Search, click Search,\n"  # Explain what to do
    "   wait for the results to appear, then press Enter here..."  # Prompt
)  # End of the reminder

# ===========================================================================
# CONSTANTS: WEBSITE, FOLDERS AND DATABASE
# ===========================================================================
BASE_URL = "https://assist.dla.mil"  # Base URL of the website
START_PAGE_URL = BASE_URL + "/online/start/"  # Log-in / start page
SEARCH_PAGE_URL = (
    BASE_URL + "/AssistNet/Analyse/FilteredSearchA.aspx"
)  # Page that is opened after the log-in to run the search
DETAIL_PAGE_URL_TEMPLATE = (
    BASE_URL + "/online/doc_analysis/doc_info_general.cfm?ident_number={}"
)  # Document page URL
LOGIN_CHECK_IDENT = (
    2  # A known document ID used to verify that the session is logged in
)
KEEP_ALIVE_PATH = "/online/doc_analysis/doc_info_general.cfm?ident_number=2"  # A small read-only page the browser requests now and then
PDF_FOLDER = Path("PDFs")  # Folder where PDFs are saved
OUTPUT_FOLDER = Path("assist_data")  # Folder for script data
BROWSER_PROFILE_FOLDER = (
    OUTPUT_FOLDER / "browser_profile"
)  # Persistent browser profile (keeps you logged in)
STATE_DATABASE_FILE = (
    OUTPUT_FOLDER / "state.sqlite3"
)  # Local database: every ID and every file seen so far
DATABASE_SCHEMA = (  # Creates the tables on first use
    "CREATE TABLE IF NOT EXISTS documents ("  # IDs seen on the website
    "ident TEXT PRIMARY KEY, doc_id TEXT NOT NULL, first_seen TEXT NOT NULL, "  # Internal ID, public ID, first sight
    "last_checked TEXT NOT NULL, pdf_count INTEGER NOT NULL);"  # Last check and number of PDF links then
    "CREATE TABLE IF NOT EXISTS files ("  # PDFs saved
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, url TEXT NOT NULL, path TEXT NOT NULL, "  # Which document, which file, where
    "bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, suspect INTEGER NOT NULL, saved_at TEXT NOT NULL, "  # Size, hash, state, time
    "PRIMARY KEY (ident, dmxid));"  # One row per file of a document
    "CREATE TABLE IF NOT EXISTS refused ("  # Files the server refused to send
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, url TEXT NOT NULL, refused_at TEXT NOT NULL, reason TEXT NOT NULL, "  # What and when
    "PRIMARY KEY (ident, dmxid));"  # One row per refused file
    "CREATE TABLE IF NOT EXISTS resume ("  # The results page where the last run stopped
    "name TEXT PRIMARY KEY, page_number INTEGER NOT NULL, total_pages INTEGER NOT NULL, updated_at TEXT NOT NULL);"  # Which page, of how many, when
    "CREATE TABLE IF NOT EXISTS suspect_checks ("  # Incomplete-looking files that were downloaded a second time
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, checked_at TEXT NOT NULL, "  # Which file and when
    "PRIMARY KEY (ident, dmxid));"  # One row per file
)  # End of the schema

# ===========================================================================
# CONSTANTS: FILE NAMES
# ===========================================================================
PDF_FILE_EXTENSION = ".pdf"  # Extension of downloaded files
PARTIAL_FILE_EXTENSION = ".part"  # Extension of a file that is still being written
SUSPECT_MARKER = (
    ".suspect"  # Inserted before ".pdf" for files that still lack the end marker
)
RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)  # Names that Windows refuses for files and folders
FOLDER_NAME_MAXIMUM_LENGTH = 60  # Maximum length of a folder name
DESCRIPTION_MAXIMUM_LENGTH = 50  # Maximum length of the description part of a file name
FILE_NUMBER_MAXIMUM_LENGTH = 20  # Maximum length of the file number part of a file name
MINIMUM_DESCRIPTION_LENGTH = (
    10  # The description part of a file name is never shortened below this
)
MAXIMUM_WINDOWS_PATH_LENGTH = (
    240  # On Windows, file paths are kept below this (the hard limit is 260)
)
FALLBACK_FOLDER_PREFIX = "ident_"  # Folder name prefix if the document ID is missing
FALLBACK_DESCRIPTION = "document"  # Description if it is missing
FALLBACK_DATE_TEXT = "no-date"  # Date text if it is missing or unparseable
FALLBACK_FILE_NUMBER = "no-number"  # File number if it is missing
DATE_INPUT_FORMATS = (
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%m/%d/%Y",
    "%Y-%m-%d",
)  # Date layouts tried in turn
DATE_OUTPUT_FORMAT = "%Y-%m-%d"  # Date layout used in file names
HASH_CHUNK_BYTES = 1024 * 1024  # Piece size when hashing a saved PDF
MAXIMUM_NON_PDF_BYTES = (
    2 * 1024 * 1024
)  # How much of a "not a PDF" file is read to recognise a login page
PDF_SIGNATURE = b"%PDF-"  # Start of every PDF
PDF_END_MARKER = b"%%EOF"  # End of every complete PDF
PDF_SIGNATURE_SEARCH_BYTES = 1024  # Bytes read when looking for the PDF signature
PDF_END_SEARCH_BYTES = (
    8192  # Bytes read at the end of a file when looking for the end marker
)

# ===========================================================================
# CONSTANTS: PAGE ELEMENTS, JAVASCRIPT AND PATTERNS
# ===========================================================================
PASSWORD_FIELD_SELECTOR = (
    "input[type='password']"  # The password field of a log-in form
)
PASSWORD_AUTOFILLED_SELECTOR = (
    PASSWORD_FIELD_SELECTOR + ":-webkit-autofill"
)  # A password field that Chrome has autofilled
SEARCH_METHOD_PATTERN = re.compile(
    pattern=r"search\s*method", flags=re.IGNORECASE
)  # Text of the "Search Method" control
ADVANCED_SEARCH_PATTERN = re.compile(
    pattern=r"advanced\s*search", flags=re.IGNORECASE
)  # Text of the "Advanced Search" control
SEARCH_BUTTON_PATTERN = re.compile(
    pattern=r"^\s*search\s*$", flags=re.IGNORECASE
)  # Name of the Search button
SEARCH_BUTTON_FALLBACK_SELECTOR = "input[type='submit'][value='Search' i], input[type='button'][value='Search' i], button:text-is('Search')"  # Backup way to find the Search button
RESULTS_GRID_SELECTOR = "#GV"  # Search results grid
PAGE_LABEL_SELECTOR = "#lblPageCount"  # Page counter label ("Page 500 of 2329")
NEXT_PAGE_BUTTON_SELECTOR = "#btnNext"  # Next page button
GOTO_PAGE_INPUT_SELECTORS = (
    "#txtPgNum",
    "input[id*='page' i]:not([type='hidden']):not([type='button']):not([type='submit']):not([type='image'])",
)  # The page-number box (Enter in it clicks "Go to Page"), then a generic backup
GRID_ROW_XPATH = "//table[@id='GV']//tr"  # All rows of the results grid
GRID_LINK_XPATH = (
    "string(.//a[contains(@href,'fireMe')]/@href)"  # The fireMe link of a row
)
ROW_CELLS_XPATH = "./td"  # Standard cells of a row
PDF_LINK_XPATH = (
    "//a[contains(@href,'loguser_hash.cfm')]"  # All PDF download links of a page
)
PARENT_ROW_XPATH = "ancestor::tr[1]"  # The table row that contains a link
PARENT_ROW_CELLS_XPATH = "./td|./th"  # All cells of a row
FILE_NUMBER_QUERY_NAME = "dmxid"  # URL parameter that holds the file number
GRID_COLUMN_DOCUMENT_ID = 1  # Column of the public document ID in the grid
DETAIL_COLUMN_DESCRIPTION = 1  # Column of the description on the document page
DETAIL_COLUMN_DATE = 3  # Column of the date on the document page
OPEN_DOCUMENT_CALL_PATTERN = re.compile(
    pattern=r"fireMe\('(?P<ident>\d+)'\)"
)  # Extracts the ID from a JS call
PAGE_LABEL_PATTERN = re.compile(
    pattern=r"Page\s+(?P<current>\d+)\s+of\s+(?P<total>\d+)"
)  # Extracts page numbers
NON_BREAKING_SPACE = "\xa0"  # Non-breaking space character used in HTML
LOGOUT_LINK_MARKER = "logout.cfm"  # Only logged-in pages contain this
LOGIN_WORD_MARKER = b"login"  # Indicates a login page
LOGIN_PAGE_SEARCH_BYTES = 5000  # Bytes read when looking for a login page
RETRY_AFTER_HEADER_NAME = "retry-after"  # Header that tells how long to wait
HTTP_RETRYABLE_STATUSES = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
}  # Usually temporary status codes
SITE_OK = "ok"  # The site answers and we are logged in
SITE_LOGGED_OUT = "logged out"  # The site answers but we are not logged in
SITE_DOWN = "down"  # The site does not answer properly
KEEP_ALIVE_JAVASCRIPT = (  # Runs inside every page of the site: the browser itself pings, so it works while Python waits for a download
    "(() => {"  # Start of the function
    "if (window.top !== window || location.hostname !== "
    + json.dumps(urlparse(url=BASE_URL).hostname)
    + " || window.__assistKeepAlive) { return; }"  # Only the top page of the site, only once per page
    "window.__assistKeepAlive = setInterval(() => {"  # Repeat forever
    "fetch("
    + json.dumps(KEEP_ALIVE_PATH)
    + ", {credentials: 'include', cache: 'no-store'}).catch(() => {});"  # Request the small page with the log-in cookies
    "}, " + str(KEEP_ALIVE_SECONDS * 1000) + ");"  # Every KEEP_ALIVE_SECONDS
    "})();"  # End of the function
)  # End of the script
LOGIN_DONE_JAVASCRIPT = (  # True when the log-in form is gone or a logout link is present
    "() => document.querySelector(\"input[type='password']\") === null"  # No password field any more
    " || document.documentElement.innerHTML.toLowerCase().includes('logout')"  # Or a logout link exists
)  # End of the script
FIRST_IDENT_JAVASCRIPT = (  # Returns the internal ID of the first document in the grid ('' if none)
    "() => {"  # Start of the function
    "const link = document.querySelector('#GV a[href*=\"fireMe\"]');"  # First document link
    "if (link === null) { return ''; }"  # No rows at all
    "const found = /fireMe\\('(\\d+)'\\)/.exec(link.getAttribute('href') || '');"  # Extract the ID
    "return found ? found[1] : '';"  # Return the ID
    "}"  # End of the function
)  # End of the script
PAGE_CHANGE_CHECK_JAVASCRIPT = (  # True when the label shows the wanted page AND the grid was really replaced
    "(expected) => {"  # Start of the function taking {pageNumber, previousIdent}
    "const pageLabel = document.getElementById('lblPageCount');"  # The page label
    "if (pageLabel === null) { return false; }"  # No label yet
    "const labelText = pageLabel.textContent.replace(/\\u00a0/g, ' ').trim();"  # Clean label text
    "if (!labelText.startsWith('Page ' + expected.pageNumber + ' ')) { return false; }"  # Wrong page
    "if (expected.previousIdent === '') { return true; }"  # Nothing to compare with
    "const link = document.querySelector('#GV a[href*=\"fireMe\"]');"  # First document link now
    "const found = link === null ? null : /fireMe\\('(\\d+)'\\)/.exec(link.getAttribute('href') || '');"  # Its ID
    "const currentIdent = found ? found[1] : '';"  # Empty while the grid is redrawn
    "return currentIdent !== '' && currentIdent !== expected.previousIdent;"  # The grid must differ from before
    "}"  # End of the function
)  # End of the script
CLICK_DOWNLOAD_JAVASCRIPT = (  # Clicks the real PDF link and forces Chrome to download instead of display
    "(url) => {"  # Start of the function
    "const existing = Array.from(document.querySelectorAll('a[href]')).find(link => link.href === url);"  # Real link
    "const link = existing || document.createElement('a');"  # Or a temporary one
    "if (!existing) { link.href = url; document.body.appendChild(link); }"  # Add the temporary link
    "link.setAttribute('download', '');"  # Download instead of display
    "link.click();"  # Click it like a person would
    "if (!existing) { link.remove(); }"  # Remove the temporary link
    "}"  # End of the function
)  # End of the script


# ===========================================================================
# DATA MODELS
# ===========================================================================
@dataclass(frozen=True, slots=True, kw_only=True)  # Immutable row of the results grid
class DocumentRecord:  # One document of the results grid
    ident: str  # Internal document ID (used in the document page URL)
    doc_id: str  # Public document ID (used as the folder name)


@dataclass(
    frozen=True, slots=True, kw_only=True
)  # Immutable PDF link of a document page
class PdfLink:  # One PDF found on a document page
    url: str  # Absolute download URL
    dmxid: str  # File number taken from the URL
    description: str  # Description text from the document page
    date: str  # Date text from the document page


@dataclass(frozen=True, slots=True, kw_only=True)  # Immutable record of one problem
class FailureRecord:  # One recorded problem
    timestamp: str  # When it happened
    label: str  # Which document or file it belongs to
    url: str  # The address (or path) involved
    reason: str  # What went wrong


@dataclass(
    frozen=True, slots=True, kw_only=True
)  # Immutable record of a document that is still failing
class FailedDocument:  # A document that will be retried
    record: DocumentRecord  # The grid row of the document
    label: str  # The ORIGINAL log label (retry suffixes are added only when logging)
    attempts: int  # How many times the document has failed so far


@dataclass(
    frozen=True, slots=True, kw_only=True
)  # Immutable result of one finished PDF download
class PdfDownload:  # A PDF that Chrome saved to a temporary file
    size: int  # Number of bytes on disk
    sha256: str  # Full SHA-256 of the file
    has_end_marker: bool  # True if the file ends with %%EOF (looks complete)
    partial_path: Path  # The temporary file that holds the data


@dataclass(kw_only=True)  # Counters and lists that describe the whole run
class RunStats:  # Running counters, owned by the Supervisor and shared with the workers
    log: logging.Logger  # Where messages go
    documents: int = 0  # Documents processed
    saved: int = 0  # PDFs saved
    skipped: int = 0  # PDFs skipped because they already exist
    failed: int = 0  # Problems (documents or files that could not be downloaded)
    suspect: int = 0  # Saved PDFs that still look incomplete
    no_pdfs: int = 0  # Documents without any PDF
    bytes_saved: int = 0  # Total PDF bytes written
    recovered: int = 0  # Documents that failed first but worked later
    restarts: int = 0  # Browser session restarts
    first_run: bool = (
        False  # True when the database was empty at the start (then nothing is announced as new)
    )
    started_at: float = field(
        default_factory=time.monotonic
    )  # Moment the script started
    failures: list[FailureRecord] = field(
        default_factory=list
    )  # Every recorded problem
    suspect_files: list[str] = field(
        default_factory=list
    )  # Paths of files saved as suspect
    refused_files: list[str] = field(
        default_factory=list
    )  # Files the server refused: 'ident/file number'
    failed_documents: dict[str, FailedDocument] = field(
        default_factory=dict
    )  # Documents that still fail
    new_documents: dict[str, str] = field(
        default_factory=dict
    )  # IDs not seen in earlier runs: ident -> public ID
    updated_documents: dict[str, list[str]] = field(
        default_factory=dict
    )  # Known IDs that got new files: ident -> file numbers

    def record_failure(
        self, *, label: str, url: str, reason: str
    ) -> None:  # Count a problem and remember it
        self.failed += 1  # Count the failure
        timestamp = datetime.now().strftime(LOG_DATE_FORMAT)  # Create a timestamp
        self.failures.append(
            FailureRecord(timestamp=timestamp, label=label, url=url, reason=reason)
        )  # Remember it for the final summary
        self.log.error(msg=f"{label}: FAILED - {reason}")  # Log the failure
        if self.failed == 1:  # The very first problem of the run
            self.log.info(
                msg="First problem recorded; every problem is listed again at the end of the run"
            )  # Tell the user

    def announce_new_document(
        self, *, record: DocumentRecord
    ) -> None:  # Announce an ID that no earlier run has seen
        if (
            self.first_run or record.ident in self.new_documents
        ):  # The first run knows nothing, and each ID is announced once
            return  # Nothing to announce
        self.new_documents[record.ident] = (
            record.doc_id
        )  # Remember it for the final summary
        self.log.info(msg=f"NEW ID {record.ident} ({record.doc_id})")  # Announce it

    def announce_new_file(
        self, *, record: DocumentRecord, pdf_link: PdfLink, document_is_new: bool
    ) -> None:  # Announce a new file on an ID that earlier runs already knew
        if document_is_new:  # New IDs were announced already
            return  # Nothing more to say
        self.updated_documents.setdefault(record.ident, []).append(
            pdf_link.dmxid
        )  # Remember it for the final summary
        self.log.info(
            msg=f"NEW FILE on known ID {record.ident} ({record.doc_id}): file {pdf_link.dmxid}"
        )  # Announce it

    def totals_line(self) -> str:  # Build the one-line summary of the running totals
        elapsed_seconds = time.monotonic() - self.started_at  # Seconds since the start
        documents_per_minute = (
            self.documents / (elapsed_seconds / 60)
            if elapsed_seconds >= 60
            else float(self.documents)
        )  # Average speed
        return (  # Build the text
            f"{self.documents} documents ({documents_per_minute:.1f}/min), {self.saved} PDFs saved "  # Documents and PDFs
            f"({self.bytes_saved / BYTES_PER_MEGABYTE:.1f} MB), {self.skipped} skipped, "  # Size and skips
            f"{len(self.new_documents)} new IDs, {len(self.updated_documents)} known IDs with new files, "  # Changes
            f"{self.no_pdfs} without PDFs, {self.suspect} suspect, {len(self.refused_files)} refused, {self.failed} problems, "  # Oddities
            f"{self.recovered} recovered, {self.restarts} restarts, elapsed {format_duration(total_seconds=elapsed_seconds)}"  # Health and time
        )  # End of the text

    def log_totals(self) -> None:  # Print the current running totals
        self.log.info(msg=f"Totals: {self.totals_line()}")  # One summary line

    def log_final_report(self) -> None:  # Print every summary at the end of the run
        self.log_totals()  # The totals
        self.log.info(
            msg=f"Changes in this run: {len(self.new_documents)} new IDs, {len(self.updated_documents)} known IDs received new files"
        )  # The big picture of the changes
        for ident, doc_id in list(self.new_documents.items())[
            :MAXIMUM_FAILURES_SHOWN
        ]:  # Each new ID
            self.log.info(msg=f"  new ID {ident} ({doc_id})")  # One line per ID
        for ident, dmxids in list(self.updated_documents.items())[
            :MAXIMUM_FAILURES_SHOWN
        ]:  # Each known ID with new files
            self.log.info(
                msg=f"  known ID {ident}: new files {', '.join(dmxids)}"
            )  # One line per ID
        if self.suspect_files:  # Some PDFs look incomplete
            self.log.warning(
                msg=f"{len(self.suspect_files)} PDFs look incomplete (no %%EOF marker); they were saved with '{SUSPECT_MARKER}' in the name:"
            )  # The big picture
            for suspect_file in self.suspect_files[
                :MAXIMUM_FAILURES_SHOWN
            ]:  # Each file
                self.log.warning(msg=f"  {suspect_file}")  # One line per file
        if self.refused_files:  # Some files were refused
            self.log.warning(
                msg=f"{len(self.refused_files)} files were refused by the server (not requested again for {REFUSED_RECHECK_HOURS} hours):"
            )  # The big picture
            for refused_file in self.refused_files[
                :MAXIMUM_FAILURES_SHOWN
            ]:  # Each file
                self.log.warning(msg=f"  {refused_file}")  # One line per file
        if self.failures:  # Something went wrong
            self.log.warning(
                msg=f"{len(self.failures)} problems were recorded, {len(self.failed_documents)} documents still have problems"
            )  # The big picture
            for failure in sorted(
                self.failures[-MAXIMUM_FAILURES_SHOWN:], key=lambda item: item.label
            ):  # The most recent problems, sorted by label
                self.log.warning(
                    msg=f"  {failure.timestamp} | {failure.label} | {failure.reason} | {failure.url}"
                )  # One line per problem


@dataclass(kw_only=True)  # Progress markers that survive browser restarts
class RunProgress:  # Decides how long restarts wait and when the run gives up
    furthest_page: int = 0  # The highest results page reached in this run
    consecutive_restarts: int = (
        0  # Restarts since the last NEW results page was reached (grows the restart wait)
    )
    paging_restarts: int = (
        0  # Paging failures in a row since the last NEW results page was reached
    )
    clear_cookies_on_start: bool = (
        False  # True after a redirect loop: the next Chrome start deletes the site cookies
    )

    def note_page(
        self, *, page_number: int
    ) -> (
        None
    ):  # A results page was reached: reset the counters only for a NEW furthest page
        if (
            page_number > self.furthest_page
        ):  # Real progress (not the same page again after a restart)
            self.furthest_page = page_number  # Remember the new furthest page
            self.consecutive_restarts = (
                0  # The next restart waits only a short time again
            )
            self.paging_restarts = 0  # Paging failures start counting from zero again


# ===========================================================================
# ERRORS RAISED ON PURPOSE
# ===========================================================================
class SessionExpiredError(Exception):  # We are not (or no longer) logged in
    pass  # No custom logic needed


class ForbiddenError(Exception):  # HTTP 403: logged out OR this one file is off limits
    pass  # No custom logic needed


class RetryableFetchError(Exception):  # A temporary problem (worth retrying)
    def __init__(
        self, message: str, wait_seconds: int | None = None
    ) -> None:  # Message plus optional server wait time
        super().__init__(message)  # Store the message
        self.wait_seconds = (
            wait_seconds  # How long the server asked us to wait (or None)
        )


class PermanentFetchError(Exception):  # A problem that retrying will not fix
    pass  # No custom logic needed


class RefusedDownloadError(
    PermanentFetchError
):  # The server refused to send a file (Chrome only says 'canceled')
    pass  # No custom logic needed


class BrowserLostError(
    Exception
):  # The browser is gone or the session ended (Chrome is restarted)
    pass  # No custom logic needed


class NetworkDownError(
    BrowserLostError
):  # The internet connection was lost (Chrome is closed and restarted when ping replies)
    pass  # No custom logic needed


class PagingFailedError(
    Exception
):  # Paging to or reading a results page failed for good (Chrome is restarted a few times, then the run stops)
    pass  # No custom logic needed


# ===========================================================================
# PURE HELPERS (no shared state: everything they need is passed in)
# ===========================================================================
def create_logger() -> logging.Logger:  # Build the logger that prints to the terminal
    if hasattr(
        sys.stdout, "reconfigure"
    ):  # Some stdout objects can change their error handling
        try:  # Reconfiguring can fail
            sys.stdout.reconfigure(
                errors="replace"
            )  # Never crash on characters the console cannot show
        except (OSError, ValueError):  # The console refuses the change
            pass  # Carry on with the default behaviour
    logger = logging.getLogger(name="assist")  # The named logger
    logger.setLevel(level=logging.INFO)  # Show INFO messages and above
    logger.propagate = (
        False  # Stop the root logger from printing every line a second time
    )
    logger.handlers.clear()  # Remove old handlers so lines are never printed twice
    handler = logging.StreamHandler(stream=sys.stdout)  # Writes to the terminal
    handler.setFormatter(
        fmt=logging.Formatter(fmt=LOG_LINE_FORMAT, datefmt=LOG_DATE_FORMAT)
    )  # Apply the layout
    logger.addHandler(hdlr=handler)  # Attach the handler
    return logger  # Hand the logger back


def format_duration(*, total_seconds: float) -> str:  # Turn seconds into h:mm:ss text
    hours, remainder = divmod(int(total_seconds), 3600)  # Split off whole hours
    minutes, seconds = divmod(remainder, 60)  # Split the rest into minutes and seconds
    return f"{hours}:{minutes:02d}:{seconds:02d}"  # Build the text


def unlink_quietly(*, file_path: Path) -> None:  # Delete a file and never complain
    try:  # Deleting can fail
        file_path.unlink(missing_ok=True)  # Remove the file if it exists
    except OSError:  # Permissions, file in use, ...
        pass  # Nothing more to do


def wait_for_disk_space(
    *, log: logging.Logger
) -> None:  # PAUSE (never stop) while the disk is too full
    PDF_FOLDER.mkdir(
        parents=True, exist_ok=True
    )  # The folder must exist so its disk can be measured
    while (
        free_bytes := shutil.disk_usage(path=PDF_FOLDER).free
    ) < MINIMUM_FREE_DISK_BYTES:  # Compare with the safety margin
        log.warning(
            msg=f"Only {free_bytes // BYTES_PER_MEGABYTE} MB of disk space left. Free some space; checking again in {DISK_WAIT_SECONDS}s"
        )  # Tell the user
        time.sleep(DISK_WAIT_SECONDS)  # Wait, then measure again


def page_shows_logout_link(
    *, page: Page
) -> bool:  # Does a tab that is already open show the logout link? (no request)
    try:  # The tab may be closed or busy
        return (
            LOGOUT_LINK_MARKER in page.content().lower()
        )  # Only logged-in pages contain the logout link
    except PlaywrightError:  # The tab cannot be inspected
        return False  # Treat it as "not shown"


def pump_page(
    *, page: Page
) -> None:  # Let Playwright process events for a short moment
    try:  # The page may be closed
        page.wait_for_timeout(timeout=KEYBOARD_POLL_MS)  # Let Playwright process events
    except PlaywrightError:  # The page is closed or the browser is gone
        time.sleep(KEYBOARD_POLL_MS / 1000)  # Just sleep instead


# --- File and folder names -------------------------------------------------
def make_safe_name_part(
    *, text: str | None, maximum_length: int, fallback: str
) -> str:  # Sanitise a string for use in a path
    cleaned_text = re.sub(
        pattern=r"[^A-Za-z0-9_]", repl="_", string=str(text or "")
    )  # Replace everything except A-Z, a-z, 0-9 and _ with _
    cleaned_text = re.sub(pattern=r"_+", repl="_", string=cleaned_text).strip(
        "_"
    )  # Collapse repeated underscores and trim the ends
    shortened_text = cleaned_text[:maximum_length]  # Cut to the maximum length
    cut_inside_a_word = (
        len(cleaned_text) > maximum_length and cleaned_text[maximum_length] != "_"
    )  # Did the cut split a word?
    if (
        cut_inside_a_word and "_" in shortened_text
    ):  # A word was cut and a separator exists
        shortened_text = shortened_text.rsplit("_", 1)[
            0
        ]  # Trim back to the last separator
    safe_text = shortened_text.strip("_") or fallback.replace("-", "_").strip(
        "_"
    )  # The clean text, or the fallback if empty
    if (
        safe_text.upper() in RESERVED_WINDOWS_NAMES
    ):  # Windows cannot create CON, NUL, COM1, ...
        safe_text += "_SAFE"  # Append _SAFE so the name is allowed
    return safe_text  # Hand the safe text back


def format_date(
    *, date_text: str
) -> str:  # Reformat a date string for use in file names
    for date_format in DATE_INPUT_FORMATS:  # Try every known layout in turn
        try:  # The layout may not fit
            return datetime.strptime(date_text.strip(), date_format).strftime(
                DATE_OUTPUT_FORMAT
            )  # Parse and reformat
        except ValueError:  # This layout does not fit
            continue  # Try the next one
    return make_safe_name_part(
        text=date_text,
        maximum_length=FILE_NUMBER_MAXIMUM_LENGTH,
        fallback=FALLBACK_DATE_TEXT,
    )  # Sanitised raw text


def build_folder_name(*, record: DocumentRecord) -> str:  # Folder name of a document
    return make_safe_name_part(
        text=record.doc_id,
        maximum_length=FOLDER_NAME_MAXIMUM_LENGTH,
        fallback=FALLBACK_FOLDER_PREFIX + record.ident,
    )  # Sanitised document ID


def build_file_name(
    *, document_folder: Path, pdf_link: PdfLink
) -> str:  # File name of a PDF (the folder already carries the document ID)
    date_part = re.sub(
        pattern=r"[^A-Za-z0-9_]", repl="_", string=format_date(date_text=pdf_link.date)
    )  # The date as YYYY-MM-DD, only A-Z, a-z, 0-9 and _
    date_part = re.sub(pattern=r"_+", repl="_", string=date_part).strip(
        "_"
    )  # Collapse repeated underscores and trim the ends
    number_part = make_safe_name_part(
        text=pdf_link.dmxid,
        maximum_length=FILE_NUMBER_MAXIMUM_LENGTH,
        fallback=FALLBACK_FILE_NUMBER,
    )  # Sanitised file number
    description_limit = DESCRIPTION_MAXIMUM_LENGTH  # Start with the normal limit
    if os.name == "nt":  # Windows has a 260 character path limit
        fixed_length = (
            len(date_part)
            + len(number_part)
            + 2
            + len(SUSPECT_MARKER)
            + len(PDF_FILE_EXTENSION)
            + len(PARTIAL_FILE_EXTENSION)
        )  # Everything except the description (2 = the joining underscores)
        room_left = (
            MAXIMUM_WINDOWS_PATH_LENGTH
            - len(str(document_folder.resolve()))
            - 1
            - fixed_length
        )  # Characters left for the description
        description_limit = max(
            MINIMUM_DESCRIPTION_LENGTH, min(description_limit, room_left)
        )  # Shorten the description if needed
    description_part = make_safe_name_part(
        text=pdf_link.description,
        maximum_length=description_limit,
        fallback=FALLBACK_DESCRIPTION,
    )  # Sanitised description
    file_name_base = "_".join(
        filter(None, [description_part, date_part, number_part])
    )  # Join the parts with underscores (empty parts are dropped)
    file_name_base = re.sub(r"_+", "_", file_name_base).strip(
        "_"
    )  # No double underscores and none at the ends
    return file_name_base + PDF_FILE_EXTENSION  # Add the extension


def suspect_path_for(
    *, target_path: Path
) -> Path:  # The name used for PDFs that look incomplete
    return target_path.with_name(
        target_path.stem + SUSPECT_MARKER + PDF_FILE_EXTENSION
    )  # For example "x_2022-03-14_A1.suspect.pdf"


def find_files_by_number(
    *, document_folder: Path, pdf_link: PdfLink
) -> list[
    Path
]:  # Find saved files of this file number even if the rest of the name differs
    number_part = make_safe_name_part(
        text=pdf_link.dmxid,
        maximum_length=FILE_NUMBER_MAXIMUM_LENGTH,
        fallback=FALLBACK_FILE_NUMBER,
    )  # The file number as it is written in file names
    try:  # The folder may not exist or be unreadable
        return sorted(
            [
                *document_folder.glob(f"*_{number_part}{PDF_FILE_EXTENSION}"),
                *document_folder.glob(
                    f"*_{number_part}{SUSPECT_MARKER}{PDF_FILE_EXTENSION}"
                ),
            ]
        )  # Complete and incomplete-looking files with this number
    except OSError:  # The folder cannot be read
        return []  # Nothing found


def existing_file_is_valid(
    *, target_path: Path
) -> bool:  # Check that an already downloaded file is plausible
    try:  # The file system can fail
        if target_path.stat().st_size == 0:  # An empty file is broken
            return False  # Download it again
        with target_path.open(mode="rb") as existing_file:  # Open the file
            return PDF_SIGNATURE in existing_file.read(
                PDF_SIGNATURE_SEARCH_BYTES
            )  # A real PDF starts with the signature
    except OSError:  # The file cannot be read
        return False  # Treat it as broken


# --- HTTP status and content checks ---------------------------------------
def parse_retry_after_seconds(
    *, header_value: str | None
) -> int | None:  # Convert a Retry-After header into seconds
    if not header_value:  # The server sent no such header
        return None  # There is no wait time
    stripped_value = header_value.strip()  # Remove stray spaces
    if stripped_value.isdigit():  # A plain number of seconds
        seconds = int(stripped_value)  # Convert it
    else:  # Possibly a date such as "Wed, 21 Oct 2026 07:28:00 GMT"
        try:  # The date may be unreadable
            retry_moment = parsedate_to_datetime(
                data=stripped_value
            )  # Parse the HTTP date
            seconds = int(
                (retry_moment - datetime.now(tz=retry_moment.tzinfo)).total_seconds()
            )  # Seconds from now until then
        except (TypeError, ValueError, OverflowError):  # Something we do not handle
            return None  # Fall back to the normal backoff
    return max(
        0, min(seconds, MAXIMUM_RETRY_AFTER_SECONDS)
    )  # Keep the value between 0 and the maximum


def check_http_status(
    *, status_code: int, status_text: str, headers: dict[str, str], label: str
) -> None:  # Turn bad HTTP status codes into the right error
    if status_code == 401:  # Not logged in (any more)
        raise SessionExpiredError(
            f"{label}: HTTP 401 - not logged in"
        )  # Ask the caller to log in again
    if (
        status_code == 403
    ):  # Logged out OR this one file is off limits; the caller finds out which
        raise ForbiddenError(
            f"{label}: HTTP 403 - access denied"
        )  # Let the caller decide
    if status_code in HTTP_RETRYABLE_STATUSES:  # 408, 429, 5xx, ...: probably temporary
        wait_seconds = parse_retry_after_seconds(
            header_value=headers.get(RETRY_AFTER_HEADER_NAME)
        )  # Respect Retry-After if given
        raise RetryableFetchError(
            f"{label}: HTTP {status_code} {status_text} (temporary)", wait_seconds
        )  # Ask the caller to retry later
    if status_code == 204:  # Success, but no content
        raise PermanentFetchError(
            f"{label}: HTTP 204 - the server sent no content"
        )  # Nothing to use
    if not 200 <= status_code < 300:  # Any other non-2xx status (404, 410, 400, ...)
        raise PermanentFetchError(
            f"{label}: HTTP {status_code} {status_text}"
        )  # Retrying will not help


def looks_like_login_page(
    *, response_body: bytes
) -> bool:  # Guess whether the server sent a login page
    has_login_word = (
        LOGIN_WORD_MARKER in response_body[:LOGIN_PAGE_SEARCH_BYTES].lower()
    )  # Does the word "login" appear at the start?
    has_logout_link = (
        LOGOUT_LINK_MARKER.encode() in response_body.lower()
    )  # Only logged-in pages have a logout link
    return (
        has_login_word and not has_logout_link
    )  # A login page mentions "login" but has no logout link


def validate_detail_page(
    *, response_body: bytes
) -> None:  # Check a document page is real and logged in
    if not response_body.strip():  # The page is empty
        raise RetryableFetchError(
            "the document page was empty"
        )  # Empty pages are usually a glitch
    if (
        LOGOUT_LINK_MARKER.encode() not in response_body.lower()
    ):  # No logout link means we were logged out
        raise SessionExpiredError(
            "the document page has no Logout link"
        )  # Ask the caller to log in again


def raise_for_non_pdf(
    *, response_body: bytes, description: str
) -> None:  # Decide WHY a file is not a PDF (always raises)
    if looks_like_login_page(response_body=response_body):  # It is really a login page
        raise SessionExpiredError(
            "got a login page instead of a PDF"
        )  # Ask the caller to log in again
    if not response_body.strip():  # The body is empty
        raise RetryableFetchError("the PDF response was empty")  # Treat as a glitch
    raise PermanentFetchError(
        f"the downloaded file is not a PDF ({description}, {len(response_body)} bytes)"
    )  # Retrying will not help


def describe_downloaded_file(
    *, partial_path: Path
) -> (
    PdfDownload
):  # Check a file saved by Chrome and describe it (raises if it is not a PDF)
    size = partial_path.stat().st_size  # Number of bytes on disk
    with partial_path.open(mode="rb") as saved_file:  # Open the file for reading
        head = saved_file.read(PDF_SIGNATURE_SEARCH_BYTES)  # Read the beginning
        if PDF_SIGNATURE not in head:  # Not a PDF
            raise_for_non_pdf(
                response_body=head + saved_file.read(MAXIMUM_NON_PDF_BYTES),
                description="saved by Chrome",
            )  # Read enough to recognise a login page (always raises)
        saved_file.seek(
            max(0, size - PDF_END_SEARCH_BYTES)
        )  # Jump near the end of the file
        tail = saved_file.read()  # Read the last bytes
        saved_file.seek(0)  # Back to the start for the hash
        digest = hashlib.sha256()  # Running hash of the file
        while chunk := saved_file.read(HASH_CHUNK_BYTES):  # Read in pieces
            digest.update(chunk)  # Hash the piece
    return PdfDownload(
        size=size,
        sha256=digest.hexdigest(),
        has_end_marker=PDF_END_MARKER in tail,
        partial_path=partial_path,
    )  # Describe the file


# --- Parsing the web pages -------------------------------------------------
def clean_text(
    *, html_element: Any
) -> str:  # Extract and clean the text of an HTML element
    return " ".join(
        html_element.text_content().replace(NON_BREAKING_SPACE, " ").split()
    )  # Replace nbsp, collapse whitespace


def get_cell_text(
    *, cell_texts: list[str], column_index: int
) -> str:  # Safely get the text of one cell
    return (
        cell_texts[column_index] if column_index < len(cell_texts) else ""
    )  # Text if the index is valid, else empty


def parse_results_grid(
    *, html_text: str
) -> list[DocumentRecord]:  # Parse the search results HTML
    html_document = lxml_html.document_fromstring(
        html=html_text
    )  # Parse the HTML string
    records: list[DocumentRecord] = []  # The records found
    seen_idents: set[str] = (
        set()
    )  # IDs already seen on this page (duplicate rows are skipped)
    for table_row in html_document.xpath(
        GRID_ROW_XPATH
    ):  # Every row of the results table
        match_found = OPEN_DOCUMENT_CALL_PATTERN.search(
            string=table_row.xpath(GRID_LINK_XPATH) or ""
        )  # The document ID link
        if (
            not match_found or match_found.group("ident") in seen_idents
        ):  # Header row, footer row or duplicate row
            continue  # Skip it
        ident = match_found.group("ident")  # The internal document ID
        seen_idents.add(ident)  # Remember this ID
        cell_texts = [
            clean_text(html_element=cell) for cell in table_row.xpath(ROW_CELLS_XPATH)
        ]  # Clean text of all cells
        records.append(
            DocumentRecord(
                ident=ident,
                doc_id=get_cell_text(
                    cell_texts=cell_texts, column_index=GRID_COLUMN_DOCUMENT_ID
                ),
            )
        )  # Add the record
    return records  # Hand the records back


def parse_detail_page(
    *, html_text: str, page_url: str
) -> list[PdfLink]:  # Parse a document page for its PDF links
    html_document = lxml_html.document_fromstring(
        html=html_text
    )  # Parse the HTML string
    pdf_links: list[PdfLink] = []  # The links found
    seen_file_numbers: set[str] = (
        set()
    )  # File numbers already processed (duplicates are skipped)
    for link_element in html_document.xpath(
        PDF_LINK_XPATH
    ):  # Every PDF link of the page
        absolute_url = urljoin(
            base=page_url, url=link_element.get(key="href", default="")
        )  # lxml already decoded entities, so never decode again
        file_number = parse_qs(qs=urlparse(url=absolute_url).query).get(
            FILE_NUMBER_QUERY_NAME, [""]
        )[
            0
        ]  # The file number from the URL
        if not file_number:  # The link has no file number at all
            file_number = f"{FALLBACK_FILE_NUMBER}-{len(pdf_links) + 1}"  # Invent a unique one so file names never collide
        if file_number in seen_file_numbers:  # Already seen
            continue  # Skip it
        seen_file_numbers.add(file_number)  # Remember it
        parent_rows = link_element.xpath(
            PARENT_ROW_XPATH
        )  # The table row that holds the link
        cell_texts = (
            [
                clean_text(html_element=cell)
                for cell in parent_rows[0].xpath(PARENT_ROW_CELLS_XPATH)
            ]
            if parent_rows
            else []
        )  # Clean text of its cells
        pdf_links.append(
            PdfLink(
                url=absolute_url,
                dmxid=file_number,
                description=get_cell_text(
                    cell_texts=cell_texts, column_index=DETAIL_COLUMN_DESCRIPTION
                ),
                date=get_cell_text(
                    cell_texts=cell_texts, column_index=DETAIL_COLUMN_DATE
                ),
            )
        )  # Add the link
    return pdf_links  # Hand the links back


# ===========================================================================
# DATABASE (WHAT WAS SEEN AND SAVED IN EARLIER RUNS)
# ===========================================================================
class Database:  # Local SQLite database; every method logs database problems and carries on
    def __init__(
        self, *, file_path: Path, log: logging.Logger
    ) -> None:  # The database file is opened on first use
        self.file_path = file_path  # Where the database lives
        self.log = log  # Where messages go
        self.connection: sqlite3.Connection | None = None  # Opened lazily

    def _connection(self) -> sqlite3.Connection:  # Open the database once and reuse it
        if self.connection is None:  # Not opened yet
            self.file_path.parent.mkdir(
                parents=True, exist_ok=True
            )  # Make sure the folder exists
            connection = sqlite3.connect(
                database=str(self.file_path)
            )  # Open (or create) the database file
            connection.executescript(
                DATABASE_SCHEMA
            )  # Create the tables if they are missing
            self.connection = connection  # Remember the connection
        return self.connection  # Hand the connection back

    def _fetch_one(
        self, sql: str, params: tuple, on_error: Any
    ) -> Any:  # Run a query and return its first row (on_error if the database fails)
        try:  # The database can fail
            return (
                self._connection().execute(sql, params).fetchone()
            )  # The first row or None
        except sqlite3.Error as error:  # The database cannot be used
            self.log.warning(
                msg=f"Could not query {self.file_path}: {error}"
            )  # Warn and carry on
            return on_error  # The caller's safe default

    def _write(
        self, sql: str, params: tuple
    ) -> None:  # Run one statement in its own transaction
        try:  # The database can fail
            with self._connection() as connection:  # One transaction (committed on success)
                connection.execute(sql, params)  # Run the statement
        except sqlite3.Error as error:  # The database cannot be used
            self.log.warning(
                msg=f"Could not write to {self.file_path}: {error}"
            )  # Warn and carry on

    def close(self) -> None:  # Close the database at the end of the run
        if self.connection is not None:  # It was opened
            self.connection.close()  # Close it
            self.connection = None  # Forget it

    def count_rows(
        self,
    ) -> tuple[int, int]:  # How many IDs and files do earlier runs know?
        document_row = self._fetch_one(
            "SELECT COUNT(*) FROM documents", (), on_error=(0,)
        )  # IDs known
        file_row = self._fetch_one(
            "SELECT COUNT(*) FROM files", (), on_error=(0,)
        )  # Files known
        return int(document_row[0]), int(file_row[0])  # Both counts

    def document_is_known(
        self, *, ident: str
    ) -> bool:  # Was this ID seen in an earlier run?
        row = self._fetch_one(
            "SELECT 1 FROM documents WHERE ident = ? UNION SELECT 1 FROM files WHERE ident = ? LIMIT 1",
            (ident, ident),
            on_error=None,
        )  # Look it up
        return row is not None  # Known if any row exists

    def record_document_check(
        self, *, record: DocumentRecord, pdf_count: int
    ) -> None:  # Remember that an ID was checked now
        now = datetime.now().isoformat(timespec="seconds")  # The time of this check
        self._write(
            "INSERT INTO documents (ident, doc_id, first_seen, last_checked, pdf_count) VALUES (?, ?, ?, ?, ?) ON CONFLICT(ident) DO UPDATE SET doc_id = excluded.doc_id, last_checked = excluded.last_checked, pdf_count = excluded.pdf_count",
            (record.ident, record.doc_id, now, now, pdf_count),
        )  # Insert the ID, or update its last check

    def saved_path(
        self, *, ident: str, dmxid: str
    ) -> Path | None:  # Where did an earlier run save this file (None if never)
        row = self._fetch_one(
            "SELECT path FROM files WHERE ident = ? AND dmxid = ?",
            (ident, dmxid),
            on_error=None,
        )  # Look it up
        return Path(row[0]) if row else None  # The recorded path, if any

    def record_file(
        self,
        *,
        record: DocumentRecord,
        pdf_link: PdfLink,
        saved_path: Path,
        download: PdfDownload,
        suspect: bool,
    ) -> None:  # Remember one saved PDF
        self._write(
            "INSERT OR REPLACE INTO files (ident, dmxid, url, path, bytes, sha256, suspect, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.ident,
                pdf_link.dmxid,
                pdf_link.url,
                str(saved_path),
                download.size,
                download.sha256,
                int(suspect),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )  # Insert or replace the row of this file
        self._write(
            "DELETE FROM refused WHERE ident = ? AND dmxid = ?",
            (record.ident, pdf_link.dmxid),
        )  # A saved file is no longer refused

    def refused_recently(
        self, *, ident: str, dmxid: str
    ) -> bool:  # Did the server refuse this file within the last REFUSED_RECHECK_HOURS?
        row = self._fetch_one(
            "SELECT refused_at FROM refused WHERE ident = ? AND dmxid = ?",
            (ident, dmxid),
            on_error=None,
        )  # Look it up
        if row is None:  # Never refused
            return False  # Ask for it
        return datetime.now() - datetime.fromisoformat(row[0]) < timedelta(
            hours=REFUSED_RECHECK_HOURS
        )  # Recent enough to leave it alone

    def record_refused_file(
        self, *, record: DocumentRecord, pdf_link: PdfLink, reason: str
    ) -> None:  # Remember that the server refused a file
        self._write(
            "INSERT OR REPLACE INTO refused (ident, dmxid, url, refused_at, reason) VALUES (?, ?, ?, ?, ?)",
            (
                record.ident,
                pdf_link.dmxid,
                pdf_link.url,
                datetime.now().isoformat(timespec="seconds"),
                reason,
            ),
        )  # Insert or replace the row of this file

    def suspect_already_retried(
        self, *, ident: str, dmxid: str
    ) -> bool:  # Was this incomplete-looking file already downloaded a second time?
        row = self._fetch_one(
            "SELECT 1 FROM suspect_checks WHERE ident = ? AND dmxid = ?",
            (ident, dmxid),
            on_error=(1,),
        )  # Look it up (a broken database counts as "already checked")
        return row is not None  # Known if a row exists

    def record_suspect_check(
        self, *, record: DocumentRecord, pdf_link: PdfLink
    ) -> None:  # Remember that an incomplete-looking file was downloaded a second time
        self._write(
            "INSERT OR REPLACE INTO suspect_checks (ident, dmxid, checked_at) VALUES (?, ?, ?)",
            (
                record.ident,
                pdf_link.dmxid,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )  # Insert or replace the row of this file

    def clear_suspect_check(
        self, *, record: DocumentRecord, pdf_link: PdfLink
    ) -> None:  # Forget the second-download mark of a file that is complete now
        self._write(
            "DELETE FROM suspect_checks WHERE ident = ? AND dmxid = ?",
            (record.ident, pdf_link.dmxid),
        )  # Remove the row of this file

    def load_resume_page(
        self,
    ) -> int | None:  # Which results page did the last run stop at? (None if unknown)
        if not RESUME_FROM_SAVED_PAGE:  # Resuming is switched off
            return None  # Start at the page the browser shows
        row = self._fetch_one(
            "SELECT page_number FROM resume WHERE name = ?",
            (RESUME_KEY,),
            on_error=None,
        )  # Look up the saved page
        return int(row[0]) if row else None  # The saved page, if there is one

    def save_resume_page(
        self, *, page_number: int, total_pages: int
    ) -> None:  # Remember the results page that is being worked on now
        self._write(
            "INSERT OR REPLACE INTO resume (name, page_number, total_pages, updated_at) VALUES (?, ?, ?, ?)",
            (
                RESUME_KEY,
                page_number,
                total_pages,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )  # Insert or replace the single resume row

    def clear_resume_page(
        self,
    ) -> None:  # Forget the saved page (the whole search was finished)
        self._write(
            "DELETE FROM resume WHERE name = ?", (RESUME_KEY,)
        )  # Remove the resume row


# ===========================================================================
# PACER (POLITE, ADAPTIVE PAUSES BETWEEN REQUESTS)
# ===========================================================================
class Pacer:  # Holds the current delay between requests
    def __init__(self) -> None:  # Start with the normal delay
        self.delay = SECONDS_BETWEEN_REQUESTS  # The delay in effect right now

    def slow_down(self) -> None:  # Double the delay (the server complained)
        self.delay = min(
            MAXIMUM_DELAY_SECONDS, self.delay * 2
        )  # Double it, but respect the maximum

    def speed_up(self) -> None:  # Move the delay back towards normal (success)
        self.delay = max(
            SECONDS_BETWEEN_REQUESTS, self.delay * 0.8
        )  # Shrink it, but never below normal

    def pause(self) -> None:  # Wait the current delay, with random jitter
        time.sleep(
            self.delay
            * random.uniform(a=JITTER_MINIMUM_FACTOR, b=JITTER_MAXIMUM_FACTOR)
        )  # Sleep delay times a random factor


# ===========================================================================
# NETWORK MONITOR (BACKGROUND PING AND WATCHDOG)
# ===========================================================================
class NetworkMonitor:  # Pings in the background and stops Chrome when the internet stays down
    def __init__(
        self, *, log: logging.Logger
    ) -> None:  # Create the monitor (nothing runs yet)
        self.log = log  # Where messages go
        self.processes: dict[str, subprocess.Popen] = (
            {}
        )  # The running ping process of each address
        self.spawned_at: dict[str, float] = (
            {}
        )  # When each ping process was started (monotonic clock)
        self.reply_at: dict[str, float] = (
            {}
        )  # Moment of the last reply of each address (monotonic clock)
        self.started_at = (
            0.0  # Moment the pings were last (re)started (monotonic clock)
        )
        self.lock = (
            threading.Lock()
        )  # Protects the processes while they are started or stopped
        self.outage_lock = (
            threading.Lock()
        )  # Held while the watchdog stops Chrome and ping, so the main thread waits for it
        self.watch_enabled = (
            False  # True while the watchdog may stop Chrome because ping gets no reply
        )
        self.network_down = False  # True while the internet is down (then Chrome is stopped and restarted)
        self.stop_event = (
            threading.Event()
        )  # Set at the end of the run to stop the watchdog

    @staticmethod  # Needs no instance
    def available() -> bool:  # Is the ping command installed?
        return shutil.which("ping") is not None  # Found on the PATH or not

    @staticmethod  # Needs no instance
    def ping_command(
        *, target: str
    ) -> list[str]:  # The command that pings one address until it is stopped
        if sys.platform == "win32":  # Windows pings forever with -t
            return ["ping", "-t", target]  # Windows command
        return [
            "ping",
            "-n",
            target,
        ]  # Linux and macOS ping forever by default (-n: no name lookups)

    def _read_output(
        self, *, process: Any, target: str
    ) -> (
        None
    ):  # Read the output of one ping process and note every reply (runs in its own thread)
        try:  # The output stream can be closed at any moment
            for output_line in process.stdout:  # Every line ping prints
                if PING_REPLY_PATTERN.search(
                    output_line
                ):  # A reply (not "unreachable" or "timeout")
                    self.reply_at[target] = time.monotonic()  # Remember when it arrived
        except (OSError, ValueError):  # The stream was closed because ping was stopped
            return  # Nothing more to read

    def _spawn(
        self, *, target: str
    ) -> None:  # Start the ping process of one address (the caller holds self.lock)
        try:  # Starting a process can fail
            process = subprocess.Popen(
                self.ping_command(target=target),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )  # Run ping in the background and read its output line by line
        except OSError as error:  # The ping command cannot be started
            self.log.warning(
                msg=f"Could not start ping for {target}: {error}"
            )  # Explain
            return  # Nothing more to do
        self.processes[target] = process  # Remember the process
        self.spawned_at[target] = time.monotonic()  # Remember when it was started
        threading.Thread(
            target=self._read_output,
            kwargs={"process": process, "target": target},
            name=f"ping-{target}",
            daemon=True,
        ).start()  # Read its output in the background

    def _ensure_running(
        self, *, target: str
    ) -> None:  # Make sure the ping of one address runs (the caller holds self.lock)
        process = self.processes.get(target)  # Its current process, if any
        if process is not None and process.poll() is None:  # It is still running
            return  # Nothing to do
        if (
            time.monotonic() - self.spawned_at.get(target, float("-inf"))
            < PING_RESTART_MIN_SECONDS
        ):  # It was started a moment ago (ping quits at once when the network is unreachable)
            return  # Do not start it again and again
        self._spawn(target=target)  # Start it again

    def _stop_target(
        self, *, target: str
    ) -> None:  # Stop the ping of one address (the caller holds self.lock)
        process = self.processes.pop(target, None)  # Forget its process
        self.spawned_at.pop(target, None)  # Forget its start time
        if process is None:  # It was not running
            return  # Nothing to stop
        try:  # Stopping can fail
            process.kill()  # Stop it
            process.wait(timeout=2)  # Wait until it is gone
        except (OSError, subprocess.TimeoutExpired):  # Already gone or stuck
            pass  # Nothing more to do

    def _last_reply_at(
        self,
    ) -> float:  # Moment of the newest reply of any address (0 if none yet)
        return max(list(self.reply_at.values()), default=0.0)  # The newest reply

    def keep_running(
        self,
    ) -> (
        None
    ):  # Ping the first address; ping the backup address only while the first one stays silent
        with self.lock:  # One thread at a time
            primary_target = PING_TARGETS[0]  # The address that is always tried first
            self._ensure_running(
                target=primary_target
            )  # Keep its ping running (restart it if it quit)
            primary_silent_seconds = time.monotonic() - max(
                self.reply_at.get(primary_target, 0.0), self.started_at
            )  # How long the first address has been silent
            for backup_target in PING_TARGETS[1:]:  # Every backup address
                if (
                    primary_silent_seconds >= PING_BACKUP_AFTER_SECONDS
                ):  # The first address does not answer
                    if (
                        backup_target not in self.processes
                    ):  # The backup is not in use yet
                        self.log.info(
                            msg=f"No ping reply from {primary_target}, also trying {backup_target}"
                        )  # Tell the user
                    self._ensure_running(
                        target=backup_target
                    )  # Ping the backup address
                elif backup_target in self.processes:  # The first address answers again
                    self.log.info(
                        msg=f"{primary_target} replies again, {backup_target} is not needed"
                    )  # Tell the user
                    self._stop_target(target=backup_target)  # Stop the backup ping

    def stop(self) -> None:  # Stop every ping process
        with self.lock:  # One thread at a time
            for target in list(self.processes):  # Every running ping
                self._stop_target(target=target)  # Stop it

    def _restart_from_scratch(
        self,
    ) -> None:  # (Re)start the background ping from nothing
        self.stop()  # Stop the old ping processes
        with self.lock:  # One thread at a time
            self.reply_at.clear()  # No reply seen yet
            self.started_at = time.monotonic()  # The silence timer starts now
            self._spawn(target=PING_TARGETS[0])  # Start with the first address only

    def _silence_seconds(self) -> float:  # How long has ping been silent?
        return time.monotonic() - max(
            self._last_reply_at(), self.started_at
        )  # Seconds since the last reply (or since the start)

    def _wait_for_reply(
        self, *, seconds: float
    ) -> bool:  # Wait for a ping reply after ping was (re)started; True if one came
        deadline = time.monotonic() + seconds  # Moment to stop waiting
        while time.monotonic() < deadline:  # Until the time is up
            if (
                self._last_reply_at() > self.started_at
            ):  # A reply arrived after the start
                return True  # The internet works
            self.keep_running()  # Restart a ping that quit, and try the backup address if the first stays silent
            time.sleep(0.5)  # Look again in a moment
        return self._last_reply_at() > self.started_at  # One last look

    def wait_for_network(
        self,
    ) -> (
        None
    ):  # Pause (never stop) until ping gets a reply; Chrome only starts after that
        with self.outage_lock:  # Wait until the watchdog has finished stopping Chrome and ping
            self.watch_enabled = False  # The watchdog leaves Chrome alone while we wait
        if not self.available():  # Without ping the internet cannot be checked
            return  # Carry on without monitoring
        while True:  # Until ping gets a reply
            self._restart_from_scratch()  # (Re)start ping
            if self._wait_for_reply(
                seconds=PING_FIRST_REPLY_WAIT_SECONDS
            ):  # A reply came
                self.log.info(
                    msg="Ping gets replies, the internet works"
                )  # Tell the user
                return  # Chrome may start
            self.log.warning(
                msg=f"No ping reply. Waiting {PING_RETRY_WAIT_SECONDS}s, then ping is restarted"
            )  # Tell the user
            self.stop()  # Stop ping while we wait
            time.sleep(PING_RETRY_WAIT_SECONDS)  # Wait, then restart ping

    def _stop_chrome(
        self,
    ) -> None:  # Kill the Chrome processes of this script (runs in the watchdog thread)
        if psutil is None:  # psutil is not installed
            self.log.warning(
                msg="psutil is not installed, so Chrome cannot be stopped from outside (pip install psutil)"
            )  # Explain
            return  # Nothing more to do
        profile_path = os.path.normcase(
            os.path.abspath(BROWSER_PROFILE_FOLDER)
        )  # The profile folder that identifies our Chrome (normcase: Windows ignores upper/lower case)
        for process in psutil.process_iter(["cmdline"]):  # Every running process
            try:  # The process can vanish while we look at it
                for argument in (
                    process.info["cmdline"] or []
                ):  # Every command line argument
                    if (
                        argument.startswith("--user-data-dir=")
                        and os.path.normcase(os.path.abspath(argument.split("=", 1)[1]))
                        == profile_path
                    ):  # This Chrome uses our profile
                        process.kill()  # Stop it
                        break  # Next process
            except psutil.Error:  # Gone already or not allowed
                continue  # Next process

    def _watchdog_loop(
        self,
    ) -> None:  # Watch the ping replies and stop Chrome when they stay away
        while not self.stop_event.wait(
            timeout=PING_CHECK_SECONDS
        ):  # Look now and then until the run ends
            with self.outage_lock:  # The main thread waits while we handle an outage
                if (
                    not self.watch_enabled
                ):  # Chrome is not running (or ping is not available)
                    continue  # Nothing to watch
                self.keep_running()  # Restart a ping process that exited
                if (
                    self._silence_seconds() < PING_SILENCE_SECONDS
                ):  # Ping still gets replies
                    continue  # Chrome keeps going
                self.watch_enabled = False  # Handle each outage once
                self.network_down = True  # Tell the main thread
                self.log.warning(
                    msg=f"No ping reply for {PING_SILENCE_SECONDS}s. Stopping Chrome and ping; they start again when ping gets replies"
                )  # Tell the user
                self.stop()  # Stop ping
                self._stop_chrome()  # Stop Chrome, so no call stays stuck on the dead connection

    def start_watchdog(self) -> None:  # Run the watchdog in its own thread
        if not self.available():  # The ping command is missing
            self.log.warning(
                msg="The ping command was not found, so the internet is not monitored"
            )  # Explain
        if psutil is None:  # Chrome cannot be stopped from outside
            self.log.warning(
                msg="psutil is not installed: Chrome cannot be stopped when the internet goes down (pip install psutil)"
            )  # Explain the limit
        threading.Thread(
            target=self._watchdog_loop, name="network-watchdog", daemon=True
        ).start()  # Daemon: never blocks the exit

    def shutdown(self) -> None:  # Stop the watchdog and every ping process
        self.stop_event.set()  # Stop the watchdog thread
        self.stop()  # Stop the background ping


# ===========================================================================
# KEYBOARD (ONLY USED WHEN THE AUTOMATIC LOG-IN FAILS)
# ===========================================================================
class Keyboard:  # Reads typed lines in a background thread
    def __init__(self) -> None:  # Create the keyboard reader (nothing runs yet)
        self.lines: queue.Queue[str] = queue.Queue()  # Every line typed in the terminal
        self.started = False  # True once the reader thread exists
        self.closed = False  # True once the terminal input has ended

    def _reader_loop(
        self,
    ) -> None:  # The loop of the keyboard thread (works on Linux, macOS and Windows)
        while True:  # Keep reading lines
            try:  # Reading can fail
                typed_line = (
                    sys.stdin.readline()
                )  # Wait for a line (blocks only this thread)
            except (OSError, ValueError):  # The input stream is broken
                typed_line = ""  # Treat it like the end of the input
            if not typed_line:  # An empty string means the input has ended
                self.closed = True  # Remember that no more input will come
                return  # End the thread
            self.lines.put(item=typed_line)  # Hand the line to the main thread

    def _take_line(
        self,
    ) -> bool:  # Remove one typed line from the queue; True if there was one
        try:  # The queue may be empty
            self.lines.get_nowait()  # Take one line
        except queue.Empty:  # Nothing typed
            return False  # No line
        return True  # A line was typed

    def wait_for_enter(
        self, *, page: Page, message: str, check_alive: Callable[[], None]
    ) -> None:  # Block until the user presses Enter (keeps the browser alive)
        if not self.started:  # The thread does not exist yet
            self.started = True  # Remember that it exists now
            threading.Thread(
                target=self._reader_loop, name="stdin-reader", daemon=True
            ).start()  # Daemon: never blocks the exit
        while (
            self._take_line()
        ):  # Throw away Enter presses that happened before this prompt
            pass  # Keep clearing
        print(message, flush=True)  # Show the prompt
        print("\a", end="", flush=True)  # Ring the terminal bell
        while True:  # Wait for Enter
            check_alive()  # Chrome died or the internet went down while waiting
            if self._take_line():  # Enter was pressed
                return  # Leave the function
            if self.closed:  # The terminal input has ended, so nobody can press Enter
                time.sleep(CLOSED_INPUT_WAIT_SECONDS)  # Give the browser some time
                return  # Continue as if Enter was pressed
            pump_page(page=page)  # Let Playwright process events


# ===========================================================================
# BROWSER SESSION (ONE CHROME LAUNCH: TABS, LOGIN CHECKS, RETRIES, DOWNLOADS)
# ===========================================================================
def clear_chrome_restore_state(
    *, log: logging.Logger
) -> (
    None
):  # Make Chrome believe it closed cleanly and forget the old tabs, so it never restores them
    default_folder = (
        BROWSER_PROFILE_FOLDER / "Default"
    )  # The folder of Chrome's default profile
    shutil.rmtree(
        default_folder / "Sessions", ignore_errors=True
    )  # Delete the saved tabs of the last session
    preferences_file = default_folder / "Preferences"  # Chrome's settings file
    try:  # Reading and writing the file can fail
        if not preferences_file.exists():  # First start: there is nothing to fix
            return  # Done
        preferences = json.loads(
            preferences_file.read_text(encoding="utf-8")
        )  # Read the settings
        profile_settings = preferences.setdefault(
            "profile", {}
        )  # The profile section of the settings
        profile_settings["exit_type"] = "Normal"  # Pretend the last exit was normal
        profile_settings["exited_cleanly"] = True  # Pretend the last exit was clean
        preferences_file.write_text(
            json.dumps(preferences), encoding="utf-8"
        )  # Save the settings
    except (OSError, ValueError) as error:  # The file is unreadable or not valid JSON
        log.warning(
            msg=f"Could not reset Chrome's restore state: {error}"
        )  # Carry on without it


class BrowserSession:  # One Chrome launch; a new object is created for every restart, so old events can never fake a crash
    def __init__(
        self,
        *,
        context: BrowserContext,
        log: logging.Logger,
        monitor: NetworkMonitor,
        pacer: Pacer,
        progress: RunProgress,
    ) -> None:  # Wrap a launched browser
        self.context = context  # The Playwright browser context
        self.log = log  # Where messages go
        self.monitor = monitor  # Tells whether the internet is down
        self.pacer = pacer  # Adaptive delay between requests
        self.progress = (
            progress  # Remembers that cookies must be cleared after a redirect loop
        )
        self.closed = False  # True when Chrome died or was closed
        self.work_page: Page | None = (
            None  # The dedicated tab for document pages and downloads
        )

    @classmethod  # Creates a session without needing one
    def launch(
        cls,
        *,
        playwright_instance: Playwright,
        log: logging.Logger,
        monitor: NetworkMonitor,
        pacer: Pacer,
        progress: RunProgress,
    ) -> "BrowserSession":  # Launch and configure Chrome
        BROWSER_PROFILE_FOLDER.mkdir(
            parents=True, exist_ok=True
        )  # Create the browser profile folder
        clear_chrome_restore_state(
            log=log
        )  # Chrome must not offer to restore tabs from a crashed session
        try:  # The browser may not be installed
            context = playwright_instance.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_FOLDER),
                headless=not SHOW_BROWSER_WINDOW,
                accept_downloads=True,
                no_viewport=True,
                args=BROWSER_LAUNCH_ARGUMENTS,
                ignore_default_args=BROWSER_IGNORED_DEFAULT_ARGUMENTS,
            )  # Launch the persistent browser (visible, maximized, password manager on)
        except PlaywrightError as error:  # Chromium could not start
            raise RuntimeError(
                f"Could not start Chromium ({error}). Try: playwright install --with-deps chromium"
            ) from error  # The main loop retries later
        session = cls(
            context=context, log=log, monitor=monitor, pacer=pacer, progress=progress
        )  # Wrap it
        for restored_page in context.pages[
            1:
        ]:  # Every tab except the first (these could only be restored tabs)
            try:  # Closing can fail
                restored_page.close()  # Close the restored tab
            except PlaywrightError:  # The tab is already gone
                pass  # Nothing more to do
        if progress.clear_cookies_on_start:  # The last session ended in a redirect loop
            try:  # Clearing can fail
                context.clear_cookies()  # Delete the broken session cookies (the saved password logs in again)
                log.info(
                    msg="Deleted the old site cookies, logging in fresh"
                )  # Tell the user
            except PlaywrightError as error:  # Chrome refused
                log.warning(msg=f"Could not delete the cookies: {error}")  # Carry on
            progress.clear_cookies_on_start = False  # Done
        context.add_init_script(
            script=KEEP_ALIVE_JAVASCRIPT
        )  # Every page of the site keeps the session alive (never clicks Logout)
        context.on(
            event="close", f=session._on_context_closed
        )  # Find out the moment Chrome dies
        context.on(event="dialog", f=session._on_dialog)  # Accept dialogs automatically
        for existing_page in context.pages:  # Every tab that is open right now
            session._watch_for_crash(
                existing_page
            )  # A crash of this tab restarts Chrome
        context.on(
            event="page", f=session._watch_for_crash
        )  # Tabs that open later are watched too
        return session  # Hand the session back

    def _on_context_closed(
        self, _context: Any = None
    ) -> None:  # Called by Playwright when the browser or its window is gone
        self.closed = True  # Every loop stops quickly

    def _on_page_crashed(
        self, _page: Page
    ) -> None:  # Called by Playwright when a tab crashes
        self.log.warning(msg="A Chrome tab crashed")  # Tell the user
        self.closed = True  # Every loop stops and the session restarts

    def _watch_for_crash(
        self, page: Page
    ) -> (
        None
    ):  # Make a tab crash restart Chrome (the context close event does not fire for a crashed tab)
        page.on(event="crash", f=self._on_page_crashed)  # Register the handler

    def _on_dialog(self, dialog: Dialog) -> None:  # Automatically accept browser alerts
        try:  # The dialog may vanish before we answer it
            self.log.info(
                msg=f"Browser alert accepted: {dialog.message[:100]!r}"
            )  # Log the alert text so it is never a mystery
            dialog.accept()  # Click OK
        except PlaywrightError as error:  # The dialog was already gone
            self.log.warning(
                msg=f"Could not answer a browser alert: {error}"
            )  # Log and carry on

    def close(self) -> None:  # Close Chrome
        self.work_page = None  # The work tab dies with the browser
        try:  # Closing can fail
            self.context.close()  # Close the browser
        except PlaywrightError:  # The browser is already gone
            pass  # Nothing more to do

    # --- Error handling ----------------------------------------------------
    def raise_if_unusable(
        self,
    ) -> None:  # Stop the current work when Chrome is gone or the internet is down
        if self.monitor.network_down:  # The network watchdog found no internet
            raise NetworkDownError(
                "the network went down"
            )  # The main loop closes Chrome and waits for the internet
        if self.closed:  # Chrome died or was closed
            raise BrowserLostError(
                "the browser was closed or crashed"
            )  # The main loop restarts the session

    def escalate_if_fatal(
        self, error: BaseException
    ) -> (
        None
    ):  # Call inside every except block: restarts Chrome when the error is fatal
        error_text = str(error).lower()  # Compare in lower case
        if (
            REDIRECT_LOOP_MARKER in error_text
        ):  # A redirect loop never fixes itself: the session cookies are invalid
            self.log.warning(
                msg=f"Redirect loop (the session is no longer valid): {error}"
            )  # Tell the user
            self.progress.clear_cookies_on_start = (
                True  # The next Chrome start deletes the cookies and logs in again
            )
            raise BrowserLostError(
                "redirect loop (ERR_TOO_MANY_REDIRECTS): closing Chrome and logging in again"
            )  # Close Chrome and start again
        if any(
            marker in error_text for marker in FATAL_BROWSER_ERROR_MARKERS
        ):  # Chrome is closed, crashed or disconnected
            self.log.warning(msg=f"Fatal browser error: {error}")  # Tell the user
            self.closed = True  # Remember it for every other loop
        self.raise_if_unusable()  # Raises BrowserLostError (or NetworkDownError) when Chrome must restart

    # --- Tabs and navigation -----------------------------------------------
    def first_page(self) -> Page:  # The first open tab (the results grid tab)
        for open_page in self.context.pages:  # Look at every tab
            if not open_page.is_closed():  # Skip closed tabs
                return open_page  # Use the first tab that is still open
        return self.context.new_page()  # No open tab left, so open a new one

    def work_tab(self) -> Page:  # The dedicated tab for document pages and downloads
        if self.work_page is None or self.work_page.is_closed():  # Missing or closed
            self.work_page = (
                self.context.new_page()
            )  # Open a fresh tab (the results grid tab is never touched)
        return self.work_page  # Hand the tab back

    def navigate(
        self, *, url: str, label: str
    ) -> tuple[Response, bytes, str]:  # VISIT a page in Chrome
        self.log.info(msg=f"{label}: Opening {url}")  # Log the address
        started_at = time.monotonic()  # Remember when the visit started
        work_page = self.work_tab()  # The dedicated tab
        response = work_page.goto(
            url=url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS
        )  # Let Chrome load the page like a person
        if response is None:  # The navigation was replaced by another one
            raise RetryableFetchError(f"{label}: Chrome gave no response")  # Try again
        page_body = work_page.content().encode(
            encoding="utf-8"
        )  # The HTML as Chrome rendered it
        self.log.info(
            msg=f"{label}: HTTP {response.status} {response.status_text}, {len(page_body)} bytes in {time.monotonic() - started_at:.1f}s"
        )  # Log status, size, duration
        return (
            response,
            page_body,
            work_page.url,
        )  # The response, the page and where Chrome ended up

    def check_site_state(self) -> str:  # Is the site up, and are we logged in?
        try:  # The visit can fail
            response, response_body, _ = self.navigate(
                url=DETAIL_PAGE_URL_TEMPLATE.format(LOGIN_CHECK_IDENT),
                label="Login check",
            )  # Visit a test page
        except PlaywrightError as error:  # Network error or timeout
            self.escalate_if_fatal(
                error
            )  # A dead Chrome or a redirect loop must restart, not count as "site down"
            self.log.warning(msg=f"Login check failed: {error}")  # Log why
            return SITE_DOWN  # The site does not answer
        if response.status in HTTP_RETRYABLE_STATUSES:  # Overloaded or broken
            return SITE_DOWN  # The site does not answer properly
        if (
            response.ok and LOGOUT_LINK_MARKER.encode() in response_body.lower()
        ):  # 2xx and a logout link
            return SITE_OK  # Up and logged in
        return SITE_LOGGED_OUT  # Up, but not logged in

    def restart_if_logged_out(
        self, *, label: str
    ) -> None:  # Restart Chrome (and log in again) when the website logged us out
        if self.check_site_state() == SITE_LOGGED_OUT:  # The session really ended
            raise BrowserLostError(
                f"{label}: logged out"
            )  # Close Chrome, start it again, log in again and resume scraping

    def wait_for_site_recovery(
        self, *, wait_seconds: float
    ) -> None:  # Wait until the site is up and we are logged in
        failed_checks = 0  # Site checks that failed in a row
        while True:  # Until the site is healthy again
            time.sleep(wait_seconds)  # Pause
            self.raise_if_unusable()  # A dead browser cannot recover by waiting
            state = self.check_site_state()  # Is the site up? Are we logged in?
            if state == SITE_OK:  # All good
                return  # Resume
            if state == SITE_LOGGED_OUT:  # The session is gone
                raise BrowserLostError(
                    "logged out during the pause"
                )  # Close the browser and start over with an automatic log-in
            failed_checks += 1  # One more failed check
            if (
                failed_checks >= SITE_DOWN_RESTART_AFTER_CHECKS
            ):  # Waiting with an open Chrome does not help
                raise NetworkDownError(
                    "the site does not answer; closing Chrome and waiting for the internet"
                )  # The main loop closes Chrome, waits for ping replies, then starts again
            wait_seconds = min(
                MAXIMUM_BACKOFF_SECONDS, wait_seconds * 2
            )  # The site is still down: wait longer
            self.log.error(
                msg=f"The site still does not answer properly; waiting {wait_seconds}s"
            )  # Tell the user

    # --- The retry engine ---------------------------------------------------
    def _handle_login_problem(
        self, *, label: str, error: Exception, backoff_seconds: float
    ) -> (
        None
    ):  # React to a 401/403: if we are logged out, restart the browser; otherwise carry on
        site_state = self.check_site_state()  # Is the site up and are we logged in?
        if site_state == SITE_LOGGED_OUT:  # The session really ended
            raise BrowserLostError(
                f"{label}: logged out"
            )  # Close the browser and start over with an automatic log-in
        if site_state == SITE_OK and isinstance(
            error, ForbiddenError
        ):  # A 403 while the session is fine
            raise PermanentFetchError(
                f"{label}: access denied (HTTP 403) although the session is valid"
            ) from error  # This file is off limits
        time.sleep(
            backoff_seconds
        )  # False alarm or the site is down: pause, then try again

    def run_with_retries(
        self, *, label: str, attempt_function: Callable[..., Any]
    ) -> Any:  # The ONE retry loop every download uses
        last_problem = (
            "unknown problem"  # The most recent problem, for the final message
        )
        attempt_number = 0  # Normal attempts made
        while attempt_number < RETRY_ATTEMPTS:  # Try up to RETRY_ATTEMPTS times
            attempt_number += 1  # Count this attempt
            self.raise_if_unusable()  # Do not retry on a dead browser
            backoff_seconds = (
                SECONDS_PER_RETRY_STEP * attempt_number
            )  # Progressive backoff: 2s, 4s, 6s, ...
            try:  # Catch every kind of problem
                result = attempt_function(
                    attempt_label=f"{label} (try {attempt_number}/{RETRY_ATTEMPTS})"
                )  # Do the actual work
                self.pacer.speed_up()  # The server is happy, so move the delay back towards normal
                return result  # Everything is fine
            except (
                SessionExpiredError,
                ForbiddenError,
            ) as error:  # We may have been logged out
                last_problem = str(error)  # Remember the problem
                self.log.warning(msg=f"{label}: {error}")  # Tell the user
                self._handle_login_problem(
                    label=label, error=error, backoff_seconds=backoff_seconds
                )  # Restarts the browser if we are logged out, otherwise pauses
                continue  # Try again (the pause was already made)
            except RetryableFetchError as error:  # A temporary server problem
                last_problem = str(error)  # Remember the problem
                backoff_seconds = (
                    error.wait_seconds
                    if error.wait_seconds is not None
                    else backoff_seconds
                )  # Prefer the server's wait time
                self.pacer.slow_down()  # Be gentler to the server from now on
                self.log.warning(
                    msg=f"{label}: {error}; waiting {backoff_seconds}s"
                )  # Tell the user
            except PermanentFetchError:  # Retrying cannot fix this
                raise  # Pass it straight up
            except (
                PlaywrightError
            ) as error:  # Network error, timeout, crash or closed browser
                last_problem = f"network error: {error}"  # Remember the problem
                self.escalate_if_fatal(
                    error
                )  # Chrome is closed, crashed or disconnected (or a redirect loop): restart it now
                self.log.warning(
                    msg=f"{label}: {last_problem}; waiting {backoff_seconds}s"
                )  # Tell the user
                if (
                    "timeout" in str(error).lower()
                ):  # A timeout can also mean that the website logged us out (a login page never starts the download)
                    self.restart_if_logged_out(
                        label=label
                    )  # Restart Chrome if we are logged out
            if attempt_number < RETRY_ATTEMPTS:  # Only wait if another attempt follows
                time.sleep(backoff_seconds)  # Wait before trying again
        raise PermanentFetchError(
            f"{label}: gave up after {RETRY_ATTEMPTS} tries ({last_problem})"
        )  # All attempts failed

    def fetch_page(
        self, *, url: str, label: str, validate_body: Callable[..., None]
    ) -> tuple[str, bytes]:  # Visit a page in Chrome safely
        def attempt(*, attempt_label: str) -> tuple[str, bytes]:  # One attempt
            response, page_body, final_url = self.navigate(
                url=url, label=attempt_label
            )  # Let Chrome visit the page
            check_http_status(
                status_code=response.status,
                status_text=response.status_text,
                headers=response.headers,
                label=label,
            )  # Check the status code
            validate_body(
                response_body=page_body
            )  # Check the content is what we expected
            return final_url, page_body  # Hand the result back

        return self.run_with_retries(
            label=label, attempt_function=attempt
        )  # Run it with all the retry rules

    # --- Downloads ------------------------------------------------------------
    def _let_chrome_download(
        self, *, work_page: Page, url: str, label: str
    ) -> Download:  # Make Chrome download one file and wait for it to finish
        with work_page.expect_download(
            timeout=REQUEST_TIMEOUT_MS
        ) as download_info:  # Wait for Chrome to start a download
            work_page.evaluate(
                expression=CLICK_DOWNLOAD_JAVASCRIPT, arg=url
            )  # Click the real link (as a download link)
        download = download_info.value  # The download Chrome started
        failure = download.failure()  # Waits for the end; None means success
        if (
            failure and failure.lower() == "canceled"
        ):  # Chrome reports exactly this when the server answered with an error status (403, 404, 500, ...)
            raise RefusedDownloadError(
                f"{label}: the server refused the download (Chrome only reports '{failure}'; probably HTTP 403, 404 or 500)"
            )  # One attempt is enough
        if failure:  # Chrome could not finish the download for another reason
            raise RetryableFetchError(
                f"{label}: Chrome download failed ({failure})"
            )  # That may be temporary
        return download  # Hand the finished download back

    def download_pdf(
        self, *, url: str, label: str, detail_url: str, partial_path: Path
    ) -> PdfDownload:  # Let CHROME download one PDF to a temporary file
        def attempt(*, attempt_label: str) -> PdfDownload:  # One attempt
            work_page = self.work_tab()  # The dedicated tab
            if (
                work_page.url != detail_url
            ):  # The link must be clicked on the document page itself
                work_page.goto(
                    url=detail_url,
                    wait_until="domcontentloaded",
                    timeout=REQUEST_TIMEOUT_MS,
                )  # Go back to the document page
            wait_for_disk_space(
                log=self.log
            )  # Make sure there is room (waits instead of stopping)
            partial_path.parent.mkdir(
                parents=True, exist_ok=True
            )  # Make sure the folder exists
            self.log.info(
                msg=f"{attempt_label}: Chrome is downloading {url}"
            )  # Log the address
            started_at = time.monotonic()  # Remember when the download started
            try:  # Chrome reports "canceled" for 403, 404 and 500 alike
                download = self._let_chrome_download(
                    work_page=work_page, url=url, label=label
                )  # Let Chrome download the file
            except (
                RefusedDownloadError
            ):  # Is this a real refusal, or a side effect of something else?
                self.raise_if_unusable()  # A dead browser is not a refusal
                site_state = (
                    self.check_site_state()
                )  # Is the site up and are we logged in?
                if (
                    site_state == SITE_LOGGED_OUT
                ):  # The session ended, so the file was not really refused
                    raise SessionExpiredError(
                        f"{label}: the download was canceled because the session ended"
                    )  # Log in again and retry
                if (
                    site_state == SITE_DOWN
                ):  # The site is not answering, so the file was not really refused
                    raise RetryableFetchError(
                        f"{label}: the download was canceled while the site was down"
                    )  # Retry later
                raise  # The site is fine, so the file really is refused
            download.save_as(
                path=str(partial_path)
            )  # Copy Chrome's temporary download to our temporary file
            try:  # A rejected file must never stay behind
                result = describe_downloaded_file(
                    partial_path=partial_path
                )  # Check it really is a PDF
            except BaseException:  # Anything went wrong (including Ctrl+C)
                unlink_quietly(file_path=partial_path)  # Remove the rejected file
                raise  # Pass the problem on
            self.log.info(
                msg=f"{attempt_label}: Chrome saved {result.size} bytes in {time.monotonic() - started_at:.1f}s"
            )  # Log size and duration
            return result  # Hand the result back

        return self.run_with_retries(
            label=label, attempt_function=attempt
        )  # Run it with all the retry rules


# ===========================================================================
# RESULTS NAVIGATOR (LOG-IN, SEARCH AND WALKING THROUGH THE RESULTS PAGES)
# ===========================================================================
class ResultsNavigator:  # Opens the results grid and yields the documents page by page
    def __init__(
        self,
        *,
        session: BrowserSession,
        keyboard: Keyboard,
        database: Database,
        stats: RunStats,
        pacer: Pacer,
        log: logging.Logger,
    ) -> None:  # Wire up everything the navigator needs
        self.session = session  # The Chrome session
        self.keyboard = keyboard  # Used only when the automatic log-in fails
        self.database = database  # Holds the saved results page
        self.stats = stats  # Receives paging problems
        self.pacer = pacer  # Polite pauses
        self.log = log  # Where messages go
        self.jump_failed = False  # True after a page-number jump failed: the rest of this session clicks Next instead

    # --- Log-in and search ---------------------------------------------------
    def _find_results_page(
        self,
    ) -> Page | None:  # Find the tab that shows the results grid
        for open_page in self.session.context.pages:  # Every open tab
            try:  # A tab can close while we look at it
                if (
                    open_page.locator(selector=RESULTS_GRID_SELECTOR).count() > 0
                ):  # The grid exists on this tab
                    return open_page  # This is the results tab
            except PlaywrightError:  # This tab cannot be inspected
                continue  # Look at the next tab
        return None  # No tab shows the results grid

    @staticmethod  # Needs no instance
    def _password_is_filled(
        *, page: Page
    ) -> bool:  # Did Chrome autofill the saved password into the log-in form?
        try:  # The page may be busy or closed
            return (
                page.locator(selector=PASSWORD_AUTOFILLED_SELECTOR).count() > 0
            )  # Chrome marks autofilled fields
        except PlaywrightError:  # The page cannot be inspected
            return False  # Treat it as "not filled"

    def _try_auto_login(
        self, *, page: Page
    ) -> (
        bool
    ):  # Log in with the password saved in Chrome; True if we are (probably) logged in
        if page_shows_logout_link(page=page):  # Already logged in
            self.log.info(msg="Already logged in")  # Tell the user
            return True  # Nothing more to do
        try:  # Counting elements can fail
            has_login_form = (
                page.locator(selector=PASSWORD_FIELD_SELECTOR).count() > 0
            )  # Is there a password field on the page?
        except PlaywrightError:  # The page cannot be inspected
            has_login_form = False  # Treat it as "no form"
        if not has_login_form:  # No log-in form is shown
            self.log.info(
                msg="No log-in form is shown, assuming we are already logged in"
            )  # Tell the user
            return True  # The search step will show whether this is true
        deadline = (
            time.monotonic() + AUTO_LOGIN_WAIT_SECONDS
        )  # Moment to stop waiting for the autofill
        while not self._password_is_filled(
            page=page
        ):  # Wait for Chrome to fill the form
            if time.monotonic() >= deadline:  # Chrome did not fill anything in time
                self.log.warning(
                    msg="Chrome did not autofill a saved password (log in once in this browser and let Chrome save the password)"
                )  # Explain
                return False  # Fall back to the manual log-in
            pump_page(page=page)  # Let Playwright process events
        self.log.info(
            msg="Chrome filled in the saved log-in, submitting it"
        )  # Tell the user
        try:  # Clicking and waiting can fail
            page.click(
                selector=PASSWORD_FIELD_SELECTOR, timeout=AUTO_STEP_TIMEOUT_MS
            )  # A real click lets Chrome release the autofilled password to the page
            page.press(
                selector=PASSWORD_FIELD_SELECTOR,
                key="Enter",
                timeout=AUTO_STEP_TIMEOUT_MS,
            )  # Press Enter to submit the form
            page.wait_for_function(
                expression=LOGIN_DONE_JAVASCRIPT, timeout=AUTO_STEP_TIMEOUT_MS
            )  # Wait until the log-in form is gone
        except PlaywrightError as error:  # Click, key press or wait failed
            self.session.escalate_if_fatal(error)  # A dead Chrome must restart
            self.log.warning(msg=f"Automatic log-in failed: {error}")  # Explain
            return False  # Fall back to the manual log-in
        self.log.info(msg="Automatic log-in OK")  # Tell the user
        return True  # Logged in

    def _run_advanced_search(
        self, *, page: Page
    ) -> (
        Page | None
    ):  # Open the search page, choose Advanced Search, click Search; the results tab or None
        try:  # The page may not load
            page.goto(
                url=SEARCH_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=REQUEST_TIMEOUT_MS,
            )  # Open the search page
        except PlaywrightError as error:  # The page did not load
            self.session.escalate_if_fatal(error)  # A dead Chrome must restart
            self.log.warning(msg=f"Could not open the search page: {error}")  # Explain
            return None  # Fall back to the manual steps
        for step_pattern in (
            SEARCH_METHOD_PATTERN,
            ADVANCED_SEARCH_PATTERN,
        ):  # The two optional clicks (the page may already be the advanced search)
            try:  # The control may not exist
                page.get_by_text(step_pattern).first.click(
                    timeout=AUTO_OPTIONAL_CLICK_TIMEOUT_MS
                )  # Click the control with this text
                self.log.info(msg=f"Clicked '{step_pattern.pattern}'")  # Tell the user
            except PlaywrightError:  # The control was not found or not clickable
                self.log.info(
                    msg=f"'{step_pattern.pattern}' not found, continuing"
                )  # Not an error: the page may already show it
        try:  # The Search button may not be found by its role
            page.get_by_role("button", name=SEARCH_BUTTON_PATTERN).first.click(
                timeout=AUTO_STEP_TIMEOUT_MS
            )  # Click the Search button
        except PlaywrightError:  # Not found that way
            try:  # Try the backup selector
                page.locator(selector=SEARCH_BUTTON_FALLBACK_SELECTOR).first.click(
                    timeout=AUTO_STEP_TIMEOUT_MS
                )  # Click the Search button
            except PlaywrightError as error:  # Not found at all
                self.session.escalate_if_fatal(error)  # A dead Chrome must restart
                self.log.warning(msg=f"Could not click Search: {error}")  # Explain
                return None  # Fall back to the manual steps
        self.log.info(
            msg="Clicked Search, waiting for the results grid"
        )  # Tell the user
        deadline = (
            time.monotonic() + REQUEST_TIMEOUT_MS / 1000
        )  # Moment to stop waiting for the grid
        while time.monotonic() < deadline:  # Until the grid shows up
            self.session.raise_if_unusable()  # Chrome died or the internet went down while waiting
            results_page = self._find_results_page()  # Look for the grid on every tab
            if results_page is not None:  # Found it
                return results_page  # Hand the results tab back
            pump_page(page=page)  # Let Playwright process events
        self.log.warning(
            msg="The results grid did not appear after clicking Search"
        )  # Explain
        return None  # Fall back to the manual steps

    def open_results_grid(
        self,
    ) -> (
        Page
    ):  # Log in and search automatically; fall back to asking the user if that fails
        page = self.session.first_page()  # The first tab
        try:  # The start page may not load
            page.goto(
                url=START_PAGE_URL, timeout=REQUEST_TIMEOUT_MS
            )  # Go to the start page
        except PlaywrightError as error:  # The page did not load
            self.session.escalate_if_fatal(
                error
            )  # A dead Chrome or a redirect loop must restart
            self.log.warning(
                msg=f"Could not open the start page: {error}"
            )  # Tell the user
        if self._try_auto_login(page=page):  # Logged in without help from the user
            results_page = self._run_advanced_search(page=page)  # Search automatically
            if results_page is not None:  # The results grid is showing
                self.log.info(msg="Found the results grid")  # Log the success
                return results_page  # Start scraping
            self.log.warning(
                msg="Automatic search failed, switching to the manual steps"
            )  # Explain
        else:  # The automatic log-in did not work
            self.log.warning(
                msg="Automatic log-in failed, switching to the manual steps"
            )  # Explain
        message = START_INSTRUCTIONS  # The first prompt (manual fallback)
        while True:  # Until the grid is found
            self.keyboard.wait_for_enter(
                page=page, message=message, check_alive=self.session.raise_if_unusable
            )  # Wait for the user
            if (
                results_page := self._find_results_page()
            ) is not None:  # Look for the grid on every tab (the grid only exists after a log-in)
                self.log.info(msg="Found the results grid")  # Log the success
                return results_page  # Hand the results tab back
            logged_in = any(
                page_shows_logout_link(page=open_page)
                for open_page in self.session.context.pages
                if not open_page.is_closed()
            )  # Does any tab show the logout link?
            message = (
                NO_RESULTS_REMINDER if logged_in else NOT_LOGGED_IN_REMINDER
            )  # Ask for the right next step

    # --- Reading a results page ------------------------------------------------
    def _read_page_numbers(
        self, *, page: Page
    ) -> tuple[int | None, int | None]:  # Read the current and total page numbers
        try:  # The label can be missing or slow
            label_text = (
                page.locator(selector=PAGE_LABEL_SELECTOR).text_content(timeout=10_000)
                or ""
            )  # The label text
        except PlaywrightError as error:  # The label could not be read
            self.session.escalate_if_fatal(error)  # A dead Chrome must restart
            return None, None  # Report "unknown"
        match_found = PAGE_LABEL_PATTERN.search(
            string=label_text.replace(NON_BREAKING_SPACE, " ")
        )  # Find the page numbers
        if not match_found:  # The text has an unexpected shape
            return None, None  # Report "unknown"
        return int(match_found.group("current")), int(
            match_found.group("total")
        )  # The numbers as integers

    def _read_first_ident(
        self, *, page: Page
    ) -> str:  # Read the internal ID of the first document in the grid ('' if unknown)
        try:  # The page may be busy
            return str(
                page.evaluate(expression=FIRST_IDENT_JAVASCRIPT) or ""
            )  # Run the JavaScript
        except PlaywrightError as error:  # The page could not be inspected
            self.session.escalate_if_fatal(error)  # A dead Chrome must restart
            return ""  # Report "unknown"

    def _read_grid_records(
        self, *, grid_page: Page, page_number: int
    ) -> list[
        DocumentRecord
    ]:  # Read the documents of the current results page (a failed read is never returned as an empty page)
        for round_number in range(1, PAGING_MAXIMUM_ROUNDS + 1):  # Several rounds
            for attempt_number in range(
                1, RETRY_ATTEMPTS + 1
            ):  # Several tries per round
                if grid_page.is_closed():  # The results tab was closed
                    raise BrowserLostError(
                        "the results tab was closed"
                    )  # The main loop restarts the session
                self.session.raise_if_unusable()  # Chrome died
                try:  # Reading and parsing can fail
                    grid_html = grid_page.locator(
                        selector=RESULTS_GRID_SELECTOR
                    ).evaluate(
                        expression="element => element.outerHTML"
                    )  # The HTML of the grid
                    return parse_results_grid(
                        html_text=grid_html
                    )  # Parse it into records
                except (
                    PlaywrightError,
                    etree.LxmlError,
                    ValueError,
                ) as error:  # Browser or parsing problem
                    self.session.escalate_if_fatal(error)  # A dead Chrome must restart
                    self.log.warning(
                        msg=f"Reading results page {page_number} failed (round {round_number}, try {attempt_number}/{RETRY_ATTEMPTS}): {error}"
                    )  # Log it
                    time.sleep(
                        SECONDS_PER_RETRY_STEP * attempt_number
                    )  # Wait before trying again
            self.session.restart_if_logged_out(
                label=f"reading results page {page_number}"
            )  # A logged-out website empties the grid: restart Chrome and log in again
            if round_number < PAGING_MAXIMUM_ROUNDS:  # Another round follows
                wait_seconds = min(
                    MAXIMUM_BACKOFF_SECONDS, SECONDS_PER_RETRY_STEP * 2**round_number
                )  # Longer pauses between rounds
                self.log.error(
                    msg=f"Results page {page_number} could not be read; trying again in {wait_seconds}s"
                )  # Explain
                time.sleep(wait_seconds)  # Wait before the next round
        reason = f"results page {page_number} could not be read in {PAGING_MAXIMUM_ROUNDS} rounds"  # What went wrong
        self.stats.record_failure(
            label=f"results page {page_number}", url=SEARCH_PAGE_URL, reason=reason
        )  # Remember it for the final summary
        raise PagingFailedError(
            reason
        )  # The main loop restarts Chrome instead of skipping the page

    # --- Moving between results pages ---------------------------------------------
    def _find_goto_page_input(
        self, *, page: Page
    ) -> Locator | None:  # Find the box where a page number can be typed
        for (
            input_selector
        ) in GOTO_PAGE_INPUT_SELECTORS:  # Every selector that may match the box
            try:  # The page may be busy or closed
                candidate = page.locator(
                    selector=input_selector
                ).first  # The first match of this selector
                if (
                    candidate.count() > 0 and candidate.is_visible()
                ):  # It exists and a person could see it
                    return candidate  # This is the page box
            except PlaywrightError as error:  # The page cannot be inspected
                self.session.escalate_if_fatal(error)  # A dead Chrome must restart
        return None  # No page box found

    def _jump_to_page(
        self, *, page: Page, target_page: int
    ) -> (
        bool
    ):  # Type a page number into the page box and press Enter; True if the grid shows that page now
        if self.jump_failed:  # A jump already failed in this session
            return False  # Use the Next button instead
        page_box = self._find_goto_page_input(page=page)  # The box for the page number
        if page_box is None:  # Not found
            self.jump_failed = True  # Do not search for it again and again
            self.log.warning(
                msg="Could not find the 'go to page' box on the results page, clicking Next instead (check GOTO_PAGE_INPUT_SELECTORS)"
            )  # Explain
            return False  # Use the Next button instead
        previous_ident = self._read_first_ident(
            page=page
        )  # First document of the old page, to prove the grid was replaced
        self.log.info(
            msg=f"Jumping to results page {target_page} by typing the page number"
        )  # Tell the user
        try:  # Typing and waiting can fail
            page_box.click(timeout=JUMP_TIMEOUT_MS)  # Focus the box like a person would
            page_box.fill(
                str(target_page), timeout=JUMP_TIMEOUT_MS
            )  # Replace its content with the page number
            page_box.press(
                "Enter", timeout=JUMP_TIMEOUT_MS
            )  # Press Enter (the box then clicks "Go to Page")
            page.wait_for_function(
                expression=PAGE_CHANGE_CHECK_JAVASCRIPT,
                arg={"pageNumber": target_page, "previousIdent": previous_ident},
                timeout=JUMP_TIMEOUT_MS,
            )  # Wait for the label and the rows to change
        except PlaywrightError as error:  # Timeout or error during typing or waiting
            self.session.escalate_if_fatal(error)  # A dead Chrome must restart
            self.log.warning(
                msg=f"Jumping to results page {target_page} failed: {error}"
            )  # Log it
            current_now, _ = self._read_page_numbers(
                page=page
            )  # The jump may have worked, just slowly
            if current_now == target_page:  # Already on the wanted page
                return True  # Success after all
            self.session.restart_if_logged_out(
                label=f"jumping to results page {target_page}"
            )  # A logged-out website is a likely reason: restart Chrome and log in again
            self.jump_failed = True  # Click Next for the rest of this session
            return False  # The caller clicks Next instead
        self.log.info(msg=f"Jumped to results page {target_page}")  # Tell the user
        return True  # The grid shows the wanted page

    def _go_to_next_page(
        self, *, page: Page, next_page_number: int
    ) -> None:  # Click next and wait until the label AND the grid changed
        previous_ident = self._read_first_ident(
            page=page
        )  # First document of the old page, to prove the grid was replaced
        round_number = 0  # Rounds of attempts
        while True:  # Until the page changed
            round_number += 1  # Count this round
            if page.is_closed():  # The results tab was closed
                raise BrowserLostError(
                    "the results tab was closed"
                )  # The main loop restarts the session
            self.session.raise_if_unusable()  # Chrome died
            for attempt_number in range(
                1, RETRY_ATTEMPTS + 1
            ):  # Several tries per round
                try:  # Clicking or waiting can time out
                    page.click(
                        selector=NEXT_PAGE_BUTTON_SELECTOR, timeout=REQUEST_TIMEOUT_MS
                    )  # Click the next page button
                    page.wait_for_function(
                        expression=PAGE_CHANGE_CHECK_JAVASCRIPT,
                        arg={
                            "pageNumber": next_page_number,
                            "previousIdent": previous_ident,
                        },
                        timeout=REQUEST_TIMEOUT_MS,
                    )  # Wait for the label and the rows to change
                    return  # Success
                except (
                    PlaywrightError
                ) as error:  # Timeout or error during click or wait
                    self.session.escalate_if_fatal(error)  # A dead Chrome must restart
                    self.log.warning(
                        msg=f"Going to results page {next_page_number} failed (round {round_number}, try {attempt_number}/{RETRY_ATTEMPTS}): {error}"
                    )  # Log it
                    current_now, _ = self._read_page_numbers(
                        page=page
                    )  # The click may have worked, just slowly
                    if current_now == next_page_number:  # Already on the wanted page
                        return  # Do NOT click again, or a page would be skipped
                    self.session.restart_if_logged_out(
                        label=f"paging to results page {next_page_number}"
                    )  # A logged-out website is the most likely reason: restart Chrome and log in again
                    time.sleep(
                        SECONDS_PER_RETRY_STEP * attempt_number
                    )  # Short pause before the next try
            if (
                round_number >= PAGING_MAXIMUM_ROUNDS
            ):  # Enough rounds, this will not fix itself without a restart
                reason = f"paging to results page {next_page_number} failed in {round_number} rounds (did the site layout change?)"  # What went wrong
                self.stats.record_failure(
                    label=f"paging to results page {next_page_number}",
                    url=SEARCH_PAGE_URL,
                    reason=reason,
                )  # Remember it for the final summary
                raise PagingFailedError(
                    reason
                )  # The main loop restarts Chrome (and stops only after several restarts without progress)
            wait_seconds = min(
                MAXIMUM_BACKOFF_SECONDS, SECONDS_PER_RETRY_STEP * 2**round_number
            )  # Longer pauses between rounds
            self.log.error(
                msg=f"Paging to results page {next_page_number} keeps failing (round {round_number}/{PAGING_MAXIMUM_ROUNDS}); trying again in {wait_seconds}s"
            )  # Explain
            time.sleep(wait_seconds)  # Wait before the next round

    def iterate_pages(
        self, *, grid_page: Page
    ) -> Iterator[
        tuple[int, int, list[DocumentRecord]]
    ]:  # Yield the documents page by page
        current_page_number: int | None = None  # The page the grid shows now
        total_page_count: int | None = None  # The number of pages
        for attempt_number in range(
            1, RETRY_ATTEMPTS + 1
        ):  # The page counter may need a moment
            current_page_number, total_page_count = self._read_page_numbers(
                page=grid_page
            )  # Read the first page numbers
            if (
                current_page_number is not None and total_page_count is not None
            ):  # Both numbers were read
                break  # Carry on
            self.log.error(
                msg="Could not read 'Page X of Y' from the results page."
            )  # Explain
            time.sleep(SECONDS_PER_RETRY_STEP * attempt_number)  # Wait, then try again
        else:  # The counter could never be read (no Enter prompt: this must work unattended)
            raise BrowserLostError(
                "cannot read 'Page X of Y'; restarting Chrome and logging in again"
            )  # The main loop restarts Chrome
        self.log.info(
            msg=f"Starting at results page {current_page_number} of {total_page_count}"
        )  # Log where we start
        resume_page_number = (
            self.database.load_resume_page()
        )  # The page where the last run stopped
        if resume_page_number is not None:  # An earlier run saved a page
            if (
                current_page_number < resume_page_number <= total_page_count
            ):  # The saved page can be reached
                self.log.info(
                    msg=f"Resuming at results page {resume_page_number} (saved by an earlier run)"
                )  # Tell the user
                if self._jump_to_page(
                    page=grid_page, target_page=resume_page_number
                ):  # One step: type the page number and press Enter
                    current_page_number = (
                        resume_page_number  # The grid shows the saved page now
                    )
                    _, jumped_total = self._read_page_numbers(
                        page=grid_page
                    )  # The site may have changed the page total
                    if jumped_total is not None:  # The total could be read
                        total_page_count = jumped_total  # Use the fresh total
                while (
                    current_page_number < resume_page_number
                ):  # Only runs when the jump was not possible: click Next until the saved page is reached
                    self._go_to_next_page(
                        page=grid_page, next_page_number=current_page_number + 1
                    )  # Click Next without reading any documents
                    current_page_number += 1  # Count the page
                    if (
                        current_page_number % PAGES_BETWEEN_SKIP_LOGS == 0
                    ):  # Now and then
                        self.log.info(
                            msg=f"Paging forward: results page {current_page_number} of {total_page_count}"
                        )  # Show the progress
                    self.pacer.pause()  # Wait briefly to be polite to the server
            elif (
                resume_page_number > total_page_count
            ):  # The saved page does not exist any more
                self.log.warning(
                    msg=f"The saved results page {resume_page_number} is beyond the last page ({total_page_count}), starting at page {current_page_number}"
                )  # Explain
                self.database.clear_resume_page()  # Forget the useless page
        while True:  # Over all pages
            page_records = self._read_grid_records(
                grid_page=grid_page, page_number=current_page_number
            )  # The documents of this page
            if not page_records:  # Nothing was found on this page
                self.log.warning(
                    msg=f"Results page {current_page_number} contains no documents"
                )  # Just log it and carry on
            yield current_page_number, total_page_count, page_records  # Hand this page to the caller
            if current_page_number >= total_page_count:  # The last page was reached
                return  # End the generator
            self._go_to_next_page(
                page=grid_page, next_page_number=current_page_number + 1
            )  # Navigate to the next page
            current_page_number += 1  # Count the page
            _, latest_total = self._read_page_numbers(
                page=grid_page
            )  # The site may have changed the page total
            if latest_total is not None:  # The total could be read
                total_page_count = latest_total  # Use the fresh total
            self.pacer.pause()  # Wait briefly to be polite to the server


# ===========================================================================
# DOCUMENT DOWNLOADER (ONE DOCUMENT AT A TIME: READ ITS PAGE, SAVE ITS NEW PDFS)
# ===========================================================================
class DocumentDownloader:  # Checks documents and saves their PDFs
    def __init__(
        self,
        *,
        session: BrowserSession,
        database: Database,
        stats: RunStats,
        pacer: Pacer,
        log: logging.Logger,
    ) -> None:  # Wire up everything the downloader needs
        self.session = session  # The Chrome session
        self.database = database  # Remembers what was saved
        self.stats = stats  # Counters and problem lists
        self.pacer = pacer  # Polite pauses
        self.log = log  # Where messages go
        self.consecutive_failures = 0  # Failed documents in a row (circuit breaker)
        self.breaker_trips = 0  # Breaker trips in a row (grows the pause)

    # --- One PDF -------------------------------------------------------------------
    def _should_skip_existing_file(
        self,
        *,
        record: DocumentRecord,
        pdf_link: PdfLink,
        file_label: str,
        target_path: Path,
        suspect_path: Path,
    ) -> bool:  # Skip a PDF that is already saved (no request is made for it)
        recorded_path = self.database.saved_path(
            ident=record.ident, dmxid=pdf_link.dmxid
        )  # Where the database says the file is
        for candidate_path in (
            recorded_path,
            target_path,
            suspect_path,
            *find_files_by_number(
                document_folder=target_path.parent, pdf_link=pdf_link
            ),
        ):  # The recorded path, the expected names, then any file in the folder with this file number
            if candidate_path is None or not candidate_path.exists():  # Nothing there
                continue  # Try the next candidate
            if not existing_file_is_valid(
                target_path=candidate_path
            ):  # The file is broken
                self.log.warning(
                    msg=f"{file_label}: the existing file {candidate_path.name} looks broken, downloading it again"
                )  # Explain
                continue  # Try the next candidate
            if (
                SUSPECT_MARKER in candidate_path.name
                and not self.database.suspect_already_retried(
                    ident=record.ident, dmxid=pdf_link.dmxid
                )
            ):  # An incomplete-looking file that was never downloaded a second time
                self.log.info(
                    msg=f"{file_label}: {candidate_path.name} looks incomplete, downloading it once more"
                )  # Explain
                continue  # Do not skip it
            self.log.info(
                msg=f"{file_label}: already downloaded ({candidate_path.name}), skipping"
            )  # Log the skip
            self.stats.skipped += 1  # Count it
            if recorded_path is None:  # The database does not know this file yet
                try:  # Reading the file can fail
                    download = describe_downloaded_file(
                        partial_path=candidate_path
                    )  # Inspect and hash the file on the local disk
                    self.database.record_file(
                        record=record,
                        pdf_link=pdf_link,
                        saved_path=candidate_path,
                        download=download,
                        suspect=SUSPECT_MARKER in candidate_path.name,
                    )  # Add it to the database
                except (
                    OSError,
                    PermanentFetchError,
                    RetryableFetchError,
                    SessionExpiredError,
                ):  # Not readable or not a PDF
                    pass  # Leave it alone
            return True  # Skip
        return False  # Nothing usable on disk: download it

    def _record_save_failure(
        self, *, file_label: str, target_path: Path, error: OSError
    ) -> None:  # Record a disk problem while saving
        if error.errno == errno.ENOSPC:  # The disk is full
            self.stats.record_failure(
                label=file_label,
                url=str(target_path),
                reason="the disk was full (will be retried)",
            )  # Remember it
            wait_for_disk_space(
                log=self.log
            )  # Wait until space was freed instead of stopping
        else:  # Any other file problem
            self.stats.record_failure(
                label=file_label,
                url=str(target_path),
                reason=f"could not save the file: {error}",
            )  # Remember it

    def _register_saved_pdf(
        self,
        *,
        record: DocumentRecord,
        pdf_link: PdfLink,
        file_label: str,
        final_path: Path,
        download: PdfDownload,
        is_suspect: bool,
        document_is_new: bool,
    ) -> None:  # Update the totals and the database after a PDF was saved
        if is_suspect:  # The file looks incomplete
            self.stats.suspect += 1  # Count it
            self.stats.suspect_files.append(
                str(final_path)
            )  # Remember which file it is
            self.log.warning(
                msg=f"{file_label}: no %%EOF end marker, the file may be incomplete; saved as {final_path.name}"
            )  # Warn
            self.database.record_suspect_check(
                record=record, pdf_link=pdf_link
            )  # It was downloaded now, so later runs do not download it again
        else:  # The file looks complete
            self.database.clear_suspect_check(
                record=record, pdf_link=pdf_link
            )  # No second-download mark is needed any more
            unlink_quietly(
                file_path=suspect_path_for(target_path=final_path)
            )  # Remove an older incomplete-looking copy
        self.log.info(
            msg=f"{file_label}: SAVED -> {final_path} ({download.size} bytes, sha256 {download.sha256[:12]})"
        )  # Log the success
        self.stats.saved += 1  # Count the file
        self.stats.bytes_saved += download.size  # Add to the byte counter
        self.database.record_file(
            record=record,
            pdf_link=pdf_link,
            saved_path=final_path,
            download=download,
            suspect=is_suspect,
        )  # Remember it in the database
        self.stats.announce_new_file(
            record=record, pdf_link=pdf_link, document_is_new=document_is_new
        )  # Announce it if the ID was already known

    def _download_one_pdf(
        self,
        *,
        record: DocumentRecord,
        pdf_link: PdfLink,
        file_label: str,
        document_folder: Path,
        detail_url: str,
        document_is_new: bool,
    ) -> bool:  # Download and save one PDF; True if it worked
        target_path = document_folder / build_file_name(
            document_folder=document_folder, pdf_link=pdf_link
        )  # Final path
        suspect_path = suspect_path_for(
            target_path=target_path
        )  # Path used if the file looks incomplete
        partial_path = target_path.with_name(
            target_path.name + PARTIAL_FILE_EXTENSION
        )  # Temporary file
        if self._should_skip_existing_file(
            record=record,
            pdf_link=pdf_link,
            file_label=file_label,
            target_path=target_path,
            suspect_path=suspect_path,
        ):  # Already saved earlier
            return True  # Nothing to do, and no request was made
        if self.database.refused_recently(
            ident=record.ident, dmxid=pdf_link.dmxid
        ):  # The server refused this file recently
            self.log.info(
                msg=f"{file_label}: the server refused this file within the last {REFUSED_RECHECK_HOURS} hours, not asking again"
            )  # Log the skip
            return True  # Nothing to do, and no request was made
        try:  # Downloading and saving can fail
            download = self.session.download_pdf(
                url=pdf_link.url,
                label=file_label,
                detail_url=detail_url,
                partial_path=partial_path,
            )  # Let Chrome download the PDF
            is_suspect = (
                not download.has_end_marker
            )  # Complete PDFs normally end with %%EOF
            final_path = (
                suspect_path if is_suspect else target_path
            )  # Suspect files get a different name
            download.partial_path.replace(
                final_path
            )  # Rename the finished file in one atomic step
        except BrowserLostError:  # Chrome is gone (or we were logged out)
            raise  # Let the main loop restart the session (this is not a problem of this file)
        except RefusedDownloadError as error:  # The server refused to send the file
            self.database.record_refused_file(
                record=record, pdf_link=pdf_link, reason=str(error)
            )  # Remember it, so it is not requested again soon
            self.stats.refused_files.append(
                f"{record.ident}/{pdf_link.dmxid}"
            )  # Remember it for the final summary
            self.log.warning(msg=str(error))  # Tell the user
            return True  # Not a problem of this run: it is listed in the summary
        except PermanentFetchError as error:  # The download failed for good
            self.stats.record_failure(
                label=file_label, url=pdf_link.url, reason=str(error)
            )  # Remember it
            return False  # This file failed
        except OSError as error:  # Saving to disk failed
            self._record_save_failure(
                file_label=file_label, target_path=target_path, error=error
            )  # Remember it
            return False  # This file failed
        except Exception as error:  # Any other surprise must not stop the run either
            self.session.escalate_if_fatal(
                error
            )  # But a dead Chrome must restart instead of failing document after document
            self.stats.record_failure(
                label=file_label,
                url=pdf_link.url,
                reason=f"unexpected error: {error!r}",
            )  # Remember it
            return False  # This file failed
        else:  # Download and saving both worked
            self._register_saved_pdf(
                record=record,
                pdf_link=pdf_link,
                file_label=file_label,
                final_path=final_path,
                download=download,
                is_suspect=is_suspect,
                document_is_new=document_is_new,
            )  # Update totals and database
            return True  # This file worked
        finally:  # Always runs
            unlink_quietly(
                file_path=partial_path
            )  # The temporary file must never stay behind
            if (
                not self.session.closed and not self.session.monitor.network_down
            ):  # Do not waste time pausing when Chrome is about to restart
                self.pacer.pause()  # Wait briefly between file downloads

    # --- One document ------------------------------------------------------------------
    def _load_pdf_links(
        self, *, record: DocumentRecord, document_label: str
    ) -> (
        tuple[str, list[PdfLink]] | None
    ):  # Let Chrome visit a document page and read its PDF links (None on failure)
        detail_request_url = DETAIL_PAGE_URL_TEMPLATE.format(
            record.ident
        )  # Address of the document page
        try:  # Fetching can fail
            detail_url, detail_body = self.session.fetch_page(
                url=detail_request_url,
                label=f"{document_label} document page",
                validate_body=validate_detail_page,
            )  # Let Chrome visit and validate the page
        except PermanentFetchError as error:  # Every attempt failed
            self.stats.record_failure(
                label=document_label, url=detail_request_url, reason=str(error)
            )  # Remember it
            return None  # The document failed
        try:  # HTML parsing can fail on odd pages
            return detail_url, parse_detail_page(
                html_text=detail_body.decode(encoding="utf-8", errors="replace"),
                page_url=detail_url,
            )  # The final URL and the links
        except (etree.LxmlError, ValueError) as error:  # Parsing problem
            self.stats.record_failure(
                label=document_label,
                url=detail_url,
                reason=f"could not parse the document page: {error}",
            )  # Remember it
            return None  # The document failed

    def _download_one_document(
        self, *, record: DocumentRecord, document_label: str, count_document: bool
    ) -> (
        bool
    ):  # Check one ID: read its page, download only the files not saved yet; True if all worked
        if count_document:  # Retries of a failed document are not counted again
            self.stats.documents += 1  # Count this document
        loaded = self._load_pdf_links(
            record=record, document_label=document_label
        )  # The document page and its links (one request)
        if loaded is None:  # Loading failed
            return False  # The document failed
        detail_url, pdf_links = loaded  # Unpack the result
        document_is_new = (
            record.ident in self.stats.new_documents
            or not self.database.document_is_known(ident=record.ident)
        )  # Did no earlier run see this ID?
        if document_is_new:  # A new ID
            self.stats.announce_new_document(record=record)  # Announce it
        if not pdf_links:  # The page lists no PDF files
            self.stats.no_pdfs += 1  # Count it
            self.log.warning(
                msg=f"{document_label}: no PDF links found on the document page"
            )  # Log it
            self.database.record_document_check(
                record=record, pdf_count=0
            )  # Remember that it was checked
            return True  # Not an error in itself
        document_folder = PDF_FOLDER / build_folder_name(
            record=record
        )  # The folder of this document
        document_ok = True  # Whether every file of this document worked
        for file_position, pdf_link in enumerate(
            pdf_links, start=1
        ):  # Every PDF listed on the page
            file_ok = self._download_one_pdf(
                record=record,
                pdf_link=pdf_link,
                file_label=f"{document_label} file {file_position}/{len(pdf_links)}",
                document_folder=document_folder,
                detail_url=detail_url,
                document_is_new=document_is_new,
            )  # Download it unless it is already saved
            document_ok = document_ok and file_ok  # One failure fails the document
        if document_ok:  # Every file is saved
            self.database.record_document_check(
                record=record, pdf_count=len(pdf_links)
            )  # Remember that the ID was checked now
        return document_ok  # Report the result

    def _circuit_breaker(
        self, *, document_ok: bool
    ) -> None:  # Pause the run when many documents fail in a row
        if document_ok:  # A success resets everything
            self.consecutive_failures = 0  # No streak
            self.breaker_trips = 0  # The next trip starts with a short pause again
            return  # Nothing else to do
        self.consecutive_failures += 1  # One more failure in a row
        if (
            self.consecutive_failures == CIRCUIT_BREAKER_LOGIN_CHECK_AT
        ):  # Two failures in a row: check early whether the website logged us out
            self.session.restart_if_logged_out(
                label="documents keep failing"
            )  # Restart Chrome if we are logged out
        if self.consecutive_failures < CIRCUIT_BREAKER_FAILURES:  # Not enough yet
            return  # Keep going
        self.breaker_trips += 1  # Count this trip
        wait_seconds = min(
            MAXIMUM_BACKOFF_SECONDS,
            CIRCUIT_BREAKER_BASE_SECONDS * 2 ** (self.breaker_trips - 1),
        )  # Longer pause with every trip
        self.log.error(
            msg=f"{self.consecutive_failures} documents in a row failed (site down, throttling or session lost?). Pausing {wait_seconds}s"
        )  # Explain
        self.session.wait_for_site_recovery(
            wait_seconds=wait_seconds
        )  # Wait until the site is healthy (restarts Chrome if logged out or the site stays down)
        self.consecutive_failures = 0  # Start counting again
        self.log.info(msg="Resuming after the pause")  # Tell the user

    def process_document(
        self, *, record: DocumentRecord, document_label: str, log_suffix: str
    ) -> bool:  # Check one document and sort it into "done" or "still failing"
        log_label = (
            document_label + log_suffix
        )  # The label shown in the log (the stored label never gets suffixes)
        try:  # NO error may escape from a single document (except "Chrome is gone")
            document_ok = self._download_one_document(
                record=record, document_label=log_label, count_document=not log_suffix
            )  # Check the document (a retry has a suffix and is not counted again)
        except BrowserLostError:  # Chrome is gone (or we were logged out)
            raise  # Let the main loop restart the session (this is not a problem of this document)
        except Exception as error:  # Anything can go wrong
            self.session.escalate_if_fatal(
                error
            )  # A dead Chrome must restart instead of failing document after document
            self.stats.record_failure(
                label=log_label,
                url=DETAIL_PAGE_URL_TEMPLATE.format(record.ident),
                reason=f"unexpected error: {error!r}",
            )  # Remember it
            document_ok = False  # This document failed
        if document_ok:  # Every file of the document worked
            if (
                record.ident in self.stats.failed_documents
            ):  # It failed earlier in this run
                del self.stats.failed_documents[
                    record.ident
                ]  # It is not a problem any more
                self.stats.recovered += 1  # Count the recovery
        else:  # Something went wrong
            previous_failure = self.stats.failed_documents.get(
                record.ident
            )  # Earlier failure of the same document
            attempts = (
                previous_failure.attempts + 1 if previous_failure is not None else 1
            )  # How often it failed now
            self.stats.failed_documents[record.ident] = FailedDocument(
                record=record, label=document_label, attempts=attempts
            )  # Remember it for the next retry round
        self._circuit_breaker(
            document_ok=document_ok
        )  # Check for a logout after 2 failures in a row, pause after 5
        return document_ok  # Report the result

    # --- Many documents ------------------------------------------------------------------
    def retry_failed_documents(
        self, *, rounds: int, final: bool
    ) -> (
        None
    ):  # Give failed documents another chance (every few pages, and once more at the end)
        for round_number in range(1, rounds + 1):  # A few rounds
            waiting_documents = [
                failed
                for failed in self.stats.failed_documents.values()
                if final or failed.attempts < MAXIMUM_DOCUMENT_ATTEMPTS
            ]  # During the run, documents that failed too often are left for the final round
            if not waiting_documents:  # Nothing left to retry
                return  # Done
            self.log.info(
                msg=f"=== Retry round {round_number}/{rounds}: {len(waiting_documents)} documents ==="
            )  # Announce the round
            for (
                failed
            ) in waiting_documents:  # Work on a copy because the dictionary changes
                self.process_document(
                    record=failed.record,
                    document_label=failed.label,
                    log_suffix=f" (retry {round_number})",
                )  # Try again (the suffix is only for logging)
                self.pacer.pause()  # Wait briefly between documents
        if (
            final and self.stats.failed_documents
        ):  # Some documents still fail at the very end
            self.log.warning(
                msg=f"{len(self.stats.failed_documents)} documents still fail after {rounds} retry rounds"
            )  # Say so

    def process_results_page(
        self,
        *,
        grid_page: Page,
        page_number: int,
        page_records: list[DocumentRecord],
        seen_idents: set[str],
    ) -> None:  # Check every document of one results page
        processed_count = 0  # Documents processed on this page
        for position, record in enumerate(
            page_records, start=1
        ):  # Every document of the page
            if grid_page.is_closed():  # The results tab disappeared
                raise BrowserLostError(
                    "the results tab was closed"
                )  # The main loop restarts the session
            self.session.raise_if_unusable()  # Chrome died or the internet went down
            if (
                record.ident in seen_idents
            ):  # Already checked in this run (no second request)
                self.log.info(
                    msg=f"Document {record.ident} already checked in this run, skipping"
                )  # Log the skip
                continue  # Next document
            seen_idents.add(record.ident)  # Remember this document
            document_label = f"[p{page_number} {position}/{len(page_records)}] {record.doc_id or record.ident}"  # Label for the log
            self.process_document(
                record=record, document_label=document_label, log_suffix=""
            )  # Check it (errors are handled inside)
            processed_count += 1  # Count it
            if processed_count % PROGRESS_EVERY_N_DOCUMENTS == 0:  # A batch boundary
                self.stats.log_totals()  # Print the totals


# ===========================================================================
# SUPERVISOR (OWNS ALL RUN-TIME STATE AND RESTARTS CHROME WHEN NEEDED)
# ===========================================================================
class Supervisor:  # Creates every object of the run and keeps starting Chrome sessions until the whole search is finished
    def __init__(
        self, *, log: logging.Logger
    ) -> None:  # Create every shared object once; they are passed to the workers
        self.log = log  # Where messages go
        self.stats = RunStats(log=log)  # Counters and problem lists
        self.progress = RunProgress()  # Restart counters and the redirect-loop flag
        self.database = Database(
            file_path=STATE_DATABASE_FILE, log=log
        )  # What earlier runs saved
        self.pacer = Pacer()  # Adaptive pauses between requests
        self.monitor = NetworkMonitor(log=log)  # Background ping and watchdog
        self.keyboard = Keyboard()  # Only used when the automatic log-in fails
        self.stopped_early = (
            False  # True when the run gave up because paging failed again and again
        )

    def _preflight(
        self,
    ) -> (
        None
    ):  # Check the environment (warnings only, nothing here can stop the script)
        if not sys.stdin.isatty():  # The Enter prompts work best in a real terminal
            self.log.warning(
                msg="stdin is not a terminal; if input ends, the Enter prompts continue by themselves"
            )  # Warn only
        try:  # Test that we may write in the output folder
            OUTPUT_FOLDER.mkdir(
                parents=True, exist_ok=True
            )  # Make sure the folder exists
            probe_file = OUTPUT_FOLDER / ".write_test"  # A throw-away test file
            probe_file.write_text(data="ok", encoding="utf-8")  # Try to write to it
            probe_file.unlink()  # Delete it again
        except OSError as error:  # Permissions, read-only disk, ...
            self.log.warning(
                msg=f"Cannot write to {OUTPUT_FOLDER.resolve()}: {error}"
            )  # Warn and carry on
        try:  # Check the free disk space without being able to crash the start
            wait_for_disk_space(log=self.log)  # Wait here if the disk is too full
        except OSError as error:  # The disk could not be measured
            self.log.warning(
                msg=f"Could not measure the free disk space: {error}"
            )  # Warn only
        document_count, file_count = (
            self.database.count_rows()
        )  # What earlier runs know
        self.stats.first_run = (
            document_count == 0 and file_count == 0
        )  # An empty database means the very first run
        self.log.info(
            msg=f"Earlier runs know {document_count} IDs and {file_count} files. Every ID is checked again; only new files are requested."
        )  # Tell the user

    def _scrape(
        self, *, session: BrowserSession
    ) -> None:  # Log in, walk through every results page and download every document
        navigator = ResultsNavigator(
            session=session,
            keyboard=self.keyboard,
            database=self.database,
            stats=self.stats,
            pacer=self.pacer,
            log=self.log,
        )  # Log-in and paging
        downloader = DocumentDownloader(
            session=session,
            database=self.database,
            stats=self.stats,
            pacer=self.pacer,
            log=self.log,
        )  # Document and PDF handling
        grid_page = (
            navigator.open_results_grid()
        )  # Log in, search and wait until the results grid is shown
        seen_idents: set[str] = (
            set()
        )  # Documents seen in this session (a document on two pages is done once)
        for page_number, total_page_count, page_records in navigator.iterate_pages(
            grid_page=grid_page
        ):  # Every results page
            self.log.info(
                msg=f"=== Results page {page_number}/{total_page_count}: {len(page_records)} documents ==="
            )  # Log the page
            self.database.save_resume_page(
                page_number=page_number, total_pages=total_page_count
            )  # Remember this page, so a restart continues here
            self.progress.note_page(
                page_number=page_number
            )  # A new furthest page resets the restart counters
            downloader.process_results_page(
                grid_page=grid_page,
                page_number=page_number,
                page_records=page_records,
                seen_idents=seen_idents,
            )  # Download its documents
            if (
                page_number % RETRY_FAILED_EVERY_N_PAGES == 0
            ):  # Time to retry what failed on earlier pages
                downloader.retry_failed_documents(
                    rounds=1, final=False
                )  # Retry now instead of waiting for the very end
        downloader.retry_failed_documents(
            rounds=FINAL_RETRY_ROUNDS, final=True
        )  # Give every failed document another chance
        self.database.clear_resume_page()  # The whole search was finished, so the next run starts at page 1 again
        self.log.info(msg="Finished all results pages")  # Tell the user

    def _run_session(
        self, *, playwright_instance: Playwright
    ) -> bool:  # Run one complete Chrome session; True when the whole search finished
        session = BrowserSession.launch(
            playwright_instance=playwright_instance,
            log=self.log,
            monitor=self.monitor,
            pacer=self.pacer,
            progress=self.progress,
        )  # Start Chrome
        try:  # The browser must always be closed
            self._scrape(session=session)  # Do the work
            return True  # The whole run finished normally
        finally:  # Runs no matter what
            self.log.info(msg="Closing the browser")  # Log the shutdown
            session.close()  # Close Chrome

    def _restart_wait_seconds(
        self,
    ) -> int:  # Count a restart and say how long to wait before the next session
        self.stats.restarts += 1  # Count the restart for the summary
        self.progress.consecutive_restarts += 1  # Count it for the growing wait
        return min(
            MAXIMUM_RESTART_WAIT_SECONDS,
            RESTART_WAIT_SECONDS * self.progress.consecutive_restarts,
        )  # Wait longer after every restart without progress

    def _run_until_finished(
        self,
    ) -> None:  # Keep starting Chrome sessions until one finishes
        finished = False  # Becomes True when the whole run completed
        while not finished:  # Until a session finishes
            self.monitor.wait_for_network()  # Chrome only starts after ping gets replies
            self.monitor.network_down = False  # A new outage is noticed from now on
            self.monitor.watch_enabled = (
                self.monitor.available()
            )  # From now on the watchdog stops Chrome if ping stays silent
            try:  # One session
                with sync_playwright() as playwright_instance:  # Start Playwright
                    finished = self._run_session(
                        playwright_instance=playwright_instance
                    )  # Run one session
            except (
                PagingFailedError
            ) as error:  # Paging or reading a results page failed for good
                self.progress.paging_restarts += 1  # Count it
                if (
                    self.progress.paging_restarts > MAXIMUM_PAGING_RESTARTS
                ):  # Restarting did not help several times in a row
                    self.log.error(
                        msg=f"STOPPING: {error}. Restarting Chrome {MAXIMUM_PAGING_RESTARTS} times did not help. Fix the problem, then run again; finished documents and files are skipped."
                    )  # Say clearly why the run ends
                    print("\a", end="", flush=True)  # Ring the terminal bell
                    self.stopped_early = True  # Remember it for the exit code
                    return  # End the run
                wait_seconds = self._restart_wait_seconds()  # How long to wait
                self.log.warning(
                    msg=f"{error}: restarting Chrome and logging in again in {wait_seconds}s (finished documents and files are skipped)"
                )  # Explain
                time.sleep(wait_seconds)  # Wait before the next session
            except (
                NetworkDownError
            ) as error:  # The internet went away, or the site stopped answering
                self.log.warning(
                    msg=f"{error}: Chrome was closed. It starts again when ping gets replies and the run continues at the saved results page (finished documents and files are skipped)"
                )  # Explain (the waiting for ping happens at the top of the loop)
                time.sleep(
                    self._restart_wait_seconds()
                )  # Short pause, so a dead website cannot cause a restart loop even when ping works
            except (
                BrowserLostError
            ) as error:  # Chrome died, a tab crashed, a redirect loop or the session ended (logout)
                wait_seconds = self._restart_wait_seconds()  # How long to wait
                self.log.warning(
                    msg=f"{error}: restarting Chrome with an automatic log-in in {wait_seconds}s (finished documents and files are skipped)"
                )  # Explain
                time.sleep(wait_seconds)  # Wait before the next session
            except (
                Exception
            ):  # Any other crash of the session (browser closed, Chromium missing, ...)
                wait_seconds = self._restart_wait_seconds()  # How long to wait
                self.log.exception(
                    msg=f"The session crashed; restarting in {wait_seconds}s (finished documents and files are skipped)"
                )  # Log the traceback
                time.sleep(wait_seconds)  # Wait before the next session
            finally:  # Runs after every session
                self.monitor.watch_enabled = False  # No Chrome is running between sessions, so the watchdog must not stop anything

    def run(self) -> int:  # Run everything; returns the exit code
        exit_code = 0  # Assume success until something goes wrong
        try:  # The only way to stop the script is Ctrl+C
            self._preflight()  # Check terminal, write access, disk space and the database
            self.monitor.start_watchdog()  # Watch the internet in the background
            self._run_until_finished()  # Run until everything is done
        except KeyboardInterrupt:  # The user pressed Ctrl+C
            self.log.warning(
                msg="Stopped by you (Ctrl+C). Run again to continue; finished documents and files are skipped."
            )  # Log it
            exit_code = 130  # The conventional exit code for Ctrl+C
        finally:  # Runs before exiting
            self.monitor.shutdown()  # Stop the watchdog and the background ping
            self.database.close()  # Close the local database
            self.stats.log_final_report()  # Print the totals and every summary
            if (
                self.stats.failed_documents or self.stopped_early
            ):  # Some documents still have problems, or the run stopped early
                exit_code = exit_code or 1  # Make sure the exit code reports it
        return exit_code  # Hand the exit code back


def main() -> int:  # The entry point; returns the exit code
    return Supervisor(log=create_logger()).run()  # Build everything and run it


if __name__ == "__main__":  # Only when the script is run directly
    sys.exit(main())  # Run main and pass its exit code to the operating system
