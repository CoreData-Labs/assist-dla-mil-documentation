#!/bin/bash
# The shebang above must stand alone on its own line; a trailing comment would be passed to bash as a bogus argument.

# -----------------------------------------------------------------------------
# Auto Git Sync Script, production edition (strict: exits on ANY error)
# Continuously watches a Git repository and does the following:
# 1. Compresses each new or modified PDF (smallest first) as soon as it is found, then stages it.
# 2. Uses the Git index (staging area) as the "already compressed" marker.
# 3. Pushes when 25 compressed files (or about 1 GB) are staged, or when the maximum wait time has passed.
# 4. Commits BEFORE pulling, so the batch is a real commit rather than fragile staged state.
# 5. Never stages a PDF that was not compressed by this script.
# 6. Exits immediately if any command fails (run it under systemd with Restart=on-failure).
# 7. Shows EVERYTHING on the console only (no log file): this script, git, Ghostscript (page by page,
#    labelled with the PDF being compressed) and, optionally, the logs of other apps such as the downloader.
# 8. Ignores the downloader's half-finished '*.part' files, so they can never be committed.
# 9. Protects production: timeouts on every external command, a free-disk check, a startup remote check,
#    a periodic heartbeat, running statistics, and a final summary on exit.
# Tunables can be overridden from the environment, e.g.: LOG_LEVEL_NUMBER=0 bash upload.sh
# To merge the downloader's log into this one: EXTRA_LOG_FILES=/var/log/etsi-downloader.log bash upload.sh
# -----------------------------------------------------------------------------

set -Eeuo pipefail # Exit on errors, inherit the ERR trap in functions, fail on unset variables, fail on pipeline errors

readonly SCRIPT_VERSION="2.1.0"                                                                                  # Version string shown in the startup report
export LC_ALL=C                                                                                                  # Use plain byte-wise text handling so file names never confuse sort or grep
export GIT_TERMINAL_PROMPT=0                                                                                     # Never let git wait for a password on a terminal nobody is watching
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=30}" # Make SSH fail fast instead of hanging or prompting

# ---------------- Configuration ----------------

readonly REPOSITORY_DIRECTORY="${REPOSITORY_DIRECTORY:-$PWD}"                       # Repository to watch (defaults to the current directory)
readonly CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-5}"                      # Seconds to sleep between repository checks
readonly MAX_BATCH_SIZE="${MAX_BATCH_SIZE:-100}"                                    # Maximum number of files in one commit/push
readonly MAX_BATCH_BYTES="${MAX_BATCH_BYTES:-1000000000}"                           # Stop adding PDFs at about 1 GB (GitHub rejects pushes over about 2 GB)
readonly MAX_SECONDS_BETWEEN_PUSHES="${MAX_SECONDS_BETWEEN_PUSHES:-43200}"          # Force a push after this many seconds (12 hours)
readonly GITHUB_MAX_FILE_BYTES="${GITHUB_MAX_FILE_BYTES:-104857600}"                # GitHub rejects files larger than 100 MiB
readonly GHOSTSCRIPT_QUALITY="${GHOSTSCRIPT_QUALITY:-/ebook}"                       # Ghostscript preset: /screen, /ebook, /printer or /prepress
readonly GHOSTSCRIPT_TIMEOUT_SECONDS="${GHOSTSCRIPT_TIMEOUT_SECONDS:-21600}"        # Kill Ghostscript if one PDF takes longer than this
readonly GIT_LOCAL_TIMEOUT_SECONDS="${GIT_LOCAL_TIMEOUT_SECONDS:-300}"              # Time limit for local git commands (add, commit)
readonly GIT_NETWORK_TIMEOUT_SECONDS="${GIT_NETWORK_TIMEOUT_SECONDS:-3600}"         # Time limit for git commands that use the network (pull, push)
readonly MIN_FREE_DISK_MB="${MIN_FREE_DISK_MB:-1024}"                               # Exit if free disk space drops below this many megabytes
readonly HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-60}"                               # How often to log an "I am alive" status line
readonly LOG_LEVEL_NUMBER="${LOG_LEVEL_NUMBER:-0}"                                  # Log detail: 0=TRACE (everything), 1=DEBUG, 2=INFO, 3=WARN, 4=FATAL
readonly EXTRA_LOG_FILES="${EXTRA_LOG_FILES:-}"                                     # Space-separated log files of OTHER apps to merge into this console output
readonly LOCK_FILE="${LOCK_FILE:-/tmp/auto-sync${REPOSITORY_DIRECTORY//\//_}.lock}" # Lock file that prevents two copies running on one repository

# ---------------- Mutable State ----------------

WORK_DIRECTORY=""          # Private temporary directory for Ghostscript files (created at startup)
FOLLOWER_PIDS=()           # Process IDs of the background jobs that follow other apps' log files
STARTED_EPOCH=0            # Unix time when the main loop started
LAST_PUSH_EPOCH=0          # Unix time of the last successful push (or timer reset)
LAST_HEARTBEAT_EPOCH=0     # Unix time of the last heartbeat line
CYCLE_NUMBER=0             # How many monitoring cycles have run
STAGED_FILE_COUNT=0        # Number of files currently staged, refreshed every loop
STAGED_BYTES=0             # Total size in bytes of the staged files, refreshed every loop
SECONDS_SINCE_LAST_PUSH=0  # Seconds since the last successful push, refreshed every loop
TOTAL_PDFS_COMPRESSED=0    # PDFs where the compressed copy replaced the original
TOTAL_PDFS_KEPT_ORIGINAL=0 # PDFs where compression did not help, so the original was kept
TOTAL_PDFS_OVERSIZED=0     # PDFs still over the GitHub limit, ignored by Git
TOTAL_BYTES_BEFORE=0       # Combined size of all PDFs before compression
TOTAL_BYTES_AFTER=0        # Combined size of all PDFs after compression
TOTAL_PUSHES=0             # Number of successful pushes
TOTAL_FILES_PUSHED=0       # Number of files pushed in total
TOTAL_BYTES_PUSHED=0       # Number of bytes pushed in total

# ---------------- Formatting Helpers ----------------

function format_bytes() {                                                                                                                                                                                                                                                 # Print a byte count in human-readable form, e.g. 12.3 MiB
	awk -v bytes="$1" 'BEGIN { split("B KiB MiB GiB TiB", unit, " "); unit_number = 1; while (bytes >= 1024 && unit_number < 5) { bytes /= 1024; unit_number++ } number_format = (unit_number == 1) ? "%d %s" : "%.1f %s"; printf number_format, bytes, unit[unit_number] }' # Divide by 1024 until the number is small
}                                                                                                                                                                                                                                                                         # End of format_bytes

function format_duration() {                                                                             # Print a number of seconds as e.g. 1h02m03s
	local total_seconds="$1"                                                                                # The duration in seconds
	printf '%dh%02dm%02ds' $((total_seconds / 3600)) $((total_seconds % 3600 / 60)) $((total_seconds % 60)) # Split into hours, minutes and seconds
}                                                                                                        # End of format_duration

function available_megabytes() {                                                                # Print the free space (in MB) of the filesystem holding a path
	df -Pk -- "$1" | awk 'NR == 2 { print int($4 / 1024); found=1 } END { if (!found) print "0" }' # Read available space in KB and convert it to MB; print 0 if no data row is found
}                                                                                               # End of available_megabytes

# ---------------- Logging (console only) ----------------

function log_message() {                                                 # Print a timestamped log line to the console if its level is enabled
	local level_name="$1"                                                   # First argument: the level name (TRACE, DEBUG, INFO, ...)
	local message="$2"                                                      # Second argument: the text to log
	local level_number                                                      # Numeric rank of the level, used for filtering
	case "$level_name" in                                                   # Translate the level name into a number
	TRACE) level_number=0 ;;                                                # Most detailed messages
	DEBUG) level_number=1 ;;                                                # Decision, progress and other apps' output
	WARN) level_number=3 ;;                                                 # Something unusual but not fatal
	FATAL) level_number=4 ;;                                                # The script is about to exit
	*) level_number=2 ;;                                                    # INFO, ACTION, SUCCESS and anything else
	esac                                                                    # End of the level translation
	if [[ $level_number -ge $LOG_LEVEL_NUMBER ]]; then                      # Print only if this level is enabled
		printf '%(%Y-%m-%dT%H:%M:%S%z)T [%s] %s\n' -1 "$level_name" "$message" # Print: local timestamp with UTC offset, level, message
	fi                                                                      # End of the level check
}                                                                        # End of log_message

function relay_output_to_log() {                               # Turn every line on standard input into a labelled log line
	local level_name="$1"                                         # Level to log the lines at
	local source_label="$2"                                       # Label that says which program or file the line came from
	local output_line                                             # Holds one line of input
	while IFS= read -r output_line || [[ -n "$output_line" ]]; do # Read every line, including a last line without a newline
		log_message "$level_name" "[${source_label}] ${output_line}" # Log the line with its source label
	done                                                          # End of the line loop
}                                                              # End of relay_output_to_log

function run_logged_command() {                                                                                                                # Run a command with a time limit and log all of its output
	local source_label="$1"                                                                                                                       # Label shown in front of every output line
	local timeout_seconds="$2"                                                                                                                    # Kill the command if it runs longer than this
	shift 2                                                                                                                                       # Everything after the first two arguments is the command itself
	local exit_code=0                                                                                                                             # Exit code of the command (0 unless it fails)
	local started_epoch                                                                                                                           # When the command started
	local elapsed_seconds                                                                                                                         # How long the command ran
	started_epoch=$(date +%s)                                                                                                                     # Record the start time
	log_message DEBUG "[${source_label}] running (limit ${timeout_seconds}s): $*"                                                                 # Log the exact command line
	timeout --kill-after=30 "$timeout_seconds" "$@" 2>&1 | tr '\r' '\n' | relay_output_to_log DEBUG "$source_label" || exit_code=${PIPESTATUS[0]} # Run it, split progress lines, log each line, keep the command's exit code
	elapsed_seconds=$(($(date +%s) - started_epoch))                                                                                              # Work out how long it took
	if [[ $exit_code -eq 124 || $exit_code -eq 137 ]]; then                                                                                       # 124/137 mean the time limit killed the command
		log_message WARN "[${source_label}] TIMED OUT after ${timeout_seconds}s"                                                                     # Say so loudly
	fi                                                                                                                                            # End of the timeout check
	log_message DEBUG "[${source_label}] finished with exit code ${exit_code} in ${elapsed_seconds}s"                                             # Log the outcome and duration
	return "$exit_code"                                                                                                                           # Hand the exit code back to the caller
}                                                                                                                                              # End of run_logged_command

function start_following_extra_logs() {                                                                           # Merge other apps' log files into this console output
	local followed_log_file                                                                                          # Loop variable for each extra log file
	if [[ -z "$EXTRA_LOG_FILES" ]]; then                                                                             # Nothing configured
		log_message DEBUG "No extra log files configured (set EXTRA_LOG_FILES to merge other apps' logs)."              # Explain how to enable the feature
		return 0                                                                                                        # Nothing to follow
	fi                                                                                                               # End of the empty check
	if ! command -v tail >/dev/null; then                                                                            # tail -F is needed to follow files
		fail_and_exit "EXTRA_LOG_FILES is set but 'tail' is not installed."                                             # Stop because we cannot follow files
	fi                                                                                                               # End of the tail check
	for followed_log_file in $EXTRA_LOG_FILES; do                                                                    # Word splitting is intended: the list is space-separated
		log_message INFO "Following other app's log: ${followed_log_file}"                                              # Log which file is merged in
		{ tail -n 0 -F --pid=$$ -- "$followed_log_file" 2>&1 | relay_output_to_log INFO "${followed_log_file##*/}"; } & # Follow new lines until this script exits, labelled with the file name
		FOLLOWER_PIDS+=("$!")                                                                                           # Remember the background job so cleanup can stop it
	done                                                                                                             # End of the file loop
}                                                                                                                 # End of start_following_extra_logs

# ---------------- Statistics ----------------

function log_statistics() {                                                                                                                                                                                           # Log the running totals
	local saved_bytes=$((TOTAL_BYTES_BEFORE - TOTAL_BYTES_AFTER))                                                                                                                                                        # Bytes saved by compression so far
	local saved_percent=0                                                                                                                                                                                                # Percentage saved (stays 0 until something was compressed)
	local uptime_seconds=0                                                                                                                                                                                               # Seconds since the main loop started
	if [[ $TOTAL_BYTES_BEFORE -gt 0 ]]; then                                                                                                                                                                             # Avoid dividing by zero
		saved_percent=$((saved_bytes * 100 / TOTAL_BYTES_BEFORE))                                                                                                                                                           # Calculate the saving
	fi                                                                                                                                                                                                                   # End of the zero check
	if [[ $STARTED_EPOCH -ne 0 ]]; then                                                                                                                                                                                  # The loop has started
		uptime_seconds=$(($(date +%s) - STARTED_EPOCH))                                                                                                                                                                     # Calculate uptime
	fi                                                                                                                                                                                                                   # End of the uptime check
	log_message INFO "STATS: uptime=$(format_duration "$uptime_seconds") cycles=${CYCLE_NUMBER} compressed=${TOTAL_PDFS_COMPRESSED} kept-original=${TOTAL_PDFS_KEPT_ORIGINAL} oversized-ignored=${TOTAL_PDFS_OVERSIZED}" # Compression counters
	log_message INFO "STATS: PDF size before=$(format_bytes "$TOTAL_BYTES_BEFORE") after=$(format_bytes "$TOTAL_BYTES_AFTER") saved=$(format_bytes "$saved_bytes") (${saved_percent}%)"                                  # Size counters
	log_message INFO "STATS: pushes=${TOTAL_PUSHES} files-pushed=${TOTAL_FILES_PUSHED} data-pushed=$(format_bytes "$TOTAL_BYTES_PUSHED")"                                                                                # Push counters
}                                                                                                                                                                                                                     # End of log_statistics

function log_heartbeat_if_due() {                                                                                                                                                                                                                                     # Log a periodic status line so operators know the script is alive
	local current_epoch                                                                                                                                                                                                                                                  # The current time
	current_epoch=$(date +%s)                                                                                                                                                                                                                                            # Read the current time
	if [[ $((current_epoch - LAST_HEARTBEAT_EPOCH)) -lt $HEARTBEAT_SECONDS ]]; then                                                                                                                                                                                      # Not due yet
		return 0                                                                                                                                                                                                                                                            # Nothing to do
	fi                                                                                                                                                                                                                                                                   # End of the due check
	LAST_HEARTBEAT_EPOCH=$current_epoch                                                                                                                                                                                                                                  # Remember when this heartbeat happened
	log_message INFO "HEARTBEAT: alive. staged=${STAGED_FILE_COUNT}/${MAX_BATCH_SIZE} files ($(format_bytes "$STAGED_BYTES")), ${SECONDS_SINCE_LAST_PUSH}s since last push, free disk repo=$(available_megabytes .) MB temp=$(available_megabytes "$WORK_DIRECTORY") MB" # Status line
	log_statistics                                                                                                                                                                                                                                                       # Include the running totals
}                                                                                                                                                                                                                                                                     # End of log_heartbeat_if_due

# ---------------- Error Handling and Cleanup ----------------

function fail_and_exit() {                   # Log a fatal message and stop the whole script
	log_message FATAL "$1 Exiting immediately." # Tell the operator what went wrong
	exit 1                                      # Exit with a failure code (the EXIT trap cleans up)
}                                            # End of fail_and_exit

function handle_unexpected_error() {               # Called by the ERR trap when any command fails unexpectedly
	log_message FATAL "Command failed at line $1: $2" # Report the line number and the failing command
}                                                  # End of handle_unexpected_error

function cleanup_on_exit() {                                         # Called by the EXIT trap on every exit path
	local exit_code=$?                                                  # Capture the exit code first, before any other command changes it
	local follower_pid                                                  # Loop variable for background log followers
	log_message INFO "Shutting down with exit code ${exit_code}."       # Log why and how we stop
	if [[ $STARTED_EPOCH -ne 0 ]]; then                                 # Only report statistics if the main loop ran
		log_statistics                                                     # Log the final totals
	fi                                                                  # End of the statistics check
	for follower_pid in "${FOLLOWER_PIDS[@]}"; do                       # Stop every background log follower
		kill "$follower_pid" 2>/dev/null || true                           # Ignore errors if it already ended
	done                                                                # End of the follower loop
	if [[ -n "${WORK_DIRECTORY}" && -d "${WORK_DIRECTORY}" ]]; then     # Only remove the directory if it was created
		log_message DEBUG "Removing temporary directory ${WORK_DIRECTORY}" # Log the cleanup
		rm -rf -- "${WORK_DIRECTORY}"                                      # Delete the temporary directory and its contents
	fi                                                                  # End of the directory check
}                                                                    # End of cleanup_on_exit

trap 'handle_unexpected_error "$LINENO" "$BASH_COMMAND"' ERR       # Log the failing line and command on any unexpected error
trap 'log_message WARN "Stop signal received."; exit 130' INT TERM # Exit cleanly when stopped with Ctrl-C or systemctl stop
trap cleanup_on_exit EXIT                                          # Always clean up and log the summary when the script ends

# ---------------- Startup Checks ----------------

function verify_dependencies() {                                                                                                             # Make sure every required program exists
	log_message DEBUG "Checking system dependencies..."                                                                                         # Log the start of the check
	if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then                                                        # mapfile -d requires Bash 4.4 or newer
		fail_and_exit "Bash 4.4 or newer is required (found ${BASH_VERSION})."                                                                     # Stop if Bash is too old
	fi                                                                                                                                          # End of the Bash version check
	local command_name                                                                                                                          # Loop variable for the command being checked
	local command_path                                                                                                                          # Holds the path where the command was found
	for command_name in gs git stat sort head cut wc grep sed awk tr df id uname date sleep timeout mktemp mkdir cp mv rm chmod xargs flock; do # Every external command the script uses
		if ! command_path=$(command -v "$command_name"); then                                                                                      # Look the command up in PATH
			fail_and_exit "Dependency '${command_name}' is not installed or not in PATH."                                                             # Stop if it is missing
		fi                                                                                                                                         # End of the lookup check
		log_message TRACE "Found '${command_name}' at ${command_path}"                                                                             # Log where it was found
	done                                                                                                                                        # End of the dependency loop
	log_message DEBUG "All system dependencies verified."                                                                                       # Log success
}                                                                                                                                            # End of verify_dependencies

function acquire_single_instance_lock() {                                       # Refuse to run if another copy is already running
	exec 9>"${LOCK_FILE}"                                                          # Open the lock file on file descriptor 9 (kept open for the script's lifetime)
	if ! flock --nonblock 9; then                                                  # Try to take the lock without waiting
		fail_and_exit "Another instance is already running (lock file ${LOCK_FILE})." # Stop if the lock is held
	fi                                                                             # End of the lock check
	log_message DEBUG "Acquired lock ${LOCK_FILE}"                                 # Log that we own the lock
}                                                                               # End of acquire_single_instance_lock

function enter_repository_root() {                                          # Change into the top directory of the Git repository
	local repository_root                                                      # Will hold the repository's top-level path
	cd -- "${REPOSITORY_DIRECTORY}"                                            # Move into the configured directory (fails loudly if missing)
	if ! repository_root=$(git rev-parse --show-toplevel); then                # Ask Git where the repository root is
		fail_and_exit "'${REPOSITORY_DIRECTORY}' is not inside a Git repository." # Stop if this is not a Git repository
	fi                                                                         # End of the repository check
	cd -- "${repository_root}"                                                 # Work from the root so all paths are root-relative
	log_message INFO "Watching repository: ${repository_root}"                 # Log the repository being watched
}                                                                           # End of enter_repository_root

function verify_remote_access() {                                                                 # Check up front that pushing can work, before compressing anything
	local upstream_name                                                                              # Upstream branch, e.g. origin/main
	local remote_name                                                                                # Remote name, e.g. origin
	local remote_url                                                                                 # Remote URL with any credentials hidden
	if ! upstream_name=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then   # Ask Git for the upstream branch
		fail_and_exit "The current branch has no upstream. Run: git push -u origin <branch>"            # Pull and push both need an upstream
	fi                                                                                               # End of the upstream check
	remote_name="${upstream_name%%/*}"                                                               # The remote is the part before the first slash
	remote_url=$(git remote get-url "$remote_name" | sed -e 's#//[^/@]*@#//***@#')                   # Read the URL and hide user:token@ credentials
	log_message INFO "Upstream branch: ${upstream_name}; remote '${remote_name}' URL: ${remote_url}" # Log where we push to
	if ! run_logged_command "git-ls-remote" 120 git ls-remote --heads "$remote_name"; then           # Prove that the network and credentials work
		fail_and_exit "Cannot reach remote '${remote_name}' (network or credentials problem)."          # Stop now instead of after a long compression run
	fi                                                                                               # End of the remote check
}                                                                                                 # End of verify_remote_access

function prepare_work_directory() {                                # Create a private temporary directory for Ghostscript
	WORK_DIRECTORY=$(mktemp -d "${TMPDIR:-/tmp}/auto-sync.XXXXXX")    # Make a unique, writable directory (honours TMPDIR, defaults to /tmp)
	log_message DEBUG "Created temporary directory ${WORK_DIRECTORY}" # Log its location
}                                                                  # End of prepare_work_directory

function verify_free_disk_space() {                                                                                                               # Exit if the disk is nearly full, so commits and files never get corrupted
	local repository_free_megabytes                                                                                                                  # Free space where the repository lives
	local work_free_megabytes                                                                                                                        # Free space where temporary files live
	repository_free_megabytes=$(available_megabytes .)                                                                                               # Measure the repository filesystem
	work_free_megabytes=$(available_megabytes "$WORK_DIRECTORY")                                                                                     # Measure the temporary filesystem
	log_message TRACE "Free disk space: repository=${repository_free_megabytes} MB, temp=${work_free_megabytes} MB (minimum ${MIN_FREE_DISK_MB} MB)" # Log the numbers
	if [[ $repository_free_megabytes -lt $MIN_FREE_DISK_MB ]]; then                                                                                  # Repository disk is too full
		fail_and_exit "Only ${repository_free_megabytes} MB free where the repository lives (minimum ${MIN_FREE_DISK_MB} MB)."                          # Stop before the disk fills up
	fi                                                                                                                                               # End of the repository disk check
	if [[ $work_free_megabytes -lt $MIN_FREE_DISK_MB ]]; then                                                                                        # Temporary disk is too full
		fail_and_exit "Only ${work_free_megabytes} MB free in the temp directory (minimum ${MIN_FREE_DISK_MB} MB)."                                     # Stop before the disk fills up
	fi                                                                                                                                               # End of the temporary disk check
}                                                                                                                                                 # End of verify_free_disk_space

function log_environment_report() {                                                                                                                             # Log everything an operator needs to know about this run
	local pending_change_count                                                                                                                                     # Files Git sees as changed at startup
	pending_change_count=$(git status --porcelain --untracked-files=all | wc -l)                                                                                   # Count changes (staged, unstaged and untracked)
	log_message INFO "===== STARTUP REPORT ====="                                                                                                                  # Start of the report
	log_message INFO "Script version ${SCRIPT_VERSION}, PID $$, user $(id -un), host ${HOSTNAME:-unknown}"                                                         # Who is running what, where
	log_message INFO "System: $(uname -sr), bash ${BASH_VERSION}"                                                                                                  # Operating system and shell
	log_message INFO "Tools: $(git --version), ghostscript $(gs --version)"                                                                                        # Versions of the programs we depend on
	log_message INFO "Git: branch $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo none), HEAD $(git rev-parse --short HEAD 2>/dev/null || echo none)"        # Where the repository stands
	log_message INFO "Changes pending at startup: ${pending_change_count} file(s)"                                                                                 # How much work is waiting
	log_message INFO "Free disk: repository=$(available_megabytes .) MB, temp=$(available_megabytes "$WORK_DIRECTORY") MB"                                         # Disk headroom
	log_message INFO "Batch: ${MAX_BATCH_SIZE} files or ${MAX_BATCH_BYTES} bytes; max wait ${MAX_SECONDS_BETWEEN_PUSHES}s; check every ${CHECK_INTERVAL_SECONDS}s" # Push settings
	log_message INFO "Ghostscript: quality ${GHOSTSCRIPT_QUALITY}, timeout ${GHOSTSCRIPT_TIMEOUT_SECONDS}s; GitHub file limit ${GITHUB_MAX_FILE_BYTES} bytes"      # Compression settings
	log_message INFO "Timeouts: local git ${GIT_LOCAL_TIMEOUT_SECONDS}s, network git ${GIT_NETWORK_TIMEOUT_SECONDS}s; minimum free disk ${MIN_FREE_DISK_MB} MB"    # Safety settings
	log_message INFO "Logging: console only, level ${LOG_LEVEL_NUMBER}, heartbeat ${HEARTBEAT_SECONDS}s, extra logs '${EXTRA_LOG_FILES}'"                          # Logging settings
	log_message INFO "===== END OF STARTUP REPORT ====="                                                                                                           # End of the report
}                                                                                                                                                               # End of log_environment_report

# ---------------- Git Helpers ----------------

function count_staged_files() {                     # Print how many files are currently staged
	git diff --cached --name-only --no-renames | wc -l # Count staged paths (renames counted as two, matching the commit)
}                                                   # End of count_staged_files

function count_staged_bytes() {                                                                                                             # Print the total size in bytes of the staged files
	git diff --cached --name-only -z --no-renames --diff-filter=AM | xargs -0 -r stat -c %s -- | awk '{ total += $1 } END { print total + 0 }' # Sum the sizes of staged files (deletions have no size)
}                                                                                                                                           # End of count_staged_bytes

function add_local_ignore_rule() {                                                    # Add a rule to the repository's local ignore file (never committed)
	local ignore_rule="$1"                                                               # The gitignore rule to add
	local exclude_file                                                                   # Path to the repository's local ignore file
	exclude_file=$(git rev-parse --git-path info/exclude)                                # Ask Git where the local exclude file lives
	mkdir -p -- "${exclude_file%/*}"                                                     # Make sure its directory exists
	if [[ -f "$exclude_file" ]] && grep -q -x -F -- "$ignore_rule" "$exclude_file"; then # Check whether the rule is already there
		log_message DEBUG "Local ignore rule already present: ${ignore_rule}"               # Log that nothing needs to change
		return 0                                                                            # Nothing to add
	fi                                                                                   # End of the duplicate check
	printf '%s\n' "$ignore_rule" >>"$exclude_file"                                       # Append the rule on its own line
	log_message INFO "Added local ignore rule '${ignore_rule}' to ${exclude_file}"       # Log the change
}                                                                                     # End of add_local_ignore_rule

function list_pdfs_to_compress() {            # Print (NUL-separated) the smallest unstaged PDFs, up to a limit
	local maximum_files="$1"                     # How many PDFs to list at most
	{                                            # Start a group that merges two Git listings
		git ls-files -z --others --exclude-standard # New (untracked) files, NUL-separated so odd filenames are safe
		git diff --name-only -z --diff-filter=MT    # Modified files that still exist (deleted files are excluded on purpose)
	} | grep -z -i '\.pdf$' |                    # Keep only PDF paths (case-insensitive)
		xargs -0 -r stat --printf='%s\t%n\0' -- |   # Prefix each path with its size in bytes
		sort -z -n |                                # Sort by size, smallest first
		sed -z -n "1,${maximum_files}p" |           # Keep only as many as fit in the batch (reads ALL input, so sort never gets a broken pipe)
		cut -z -f2-                                 # Drop the size column, leaving only the paths
}                                             # End of list_pdfs_to_compress

function list_other_changed_files() {                                          # Print (NUL-separated) non-PDF changes plus deletions, up to a limit
	local maximum_files="$1"                                                      # How many paths to list at most
	{                                                                             # Start a group that merges three Git listings
		git ls-files -z --others --exclude-standard | grep -z -v -i '\.pdf$' || true # New non-PDF files (grep exits 1 when nothing matches, which is fine)
		git diff --name-only -z --diff-filter=MT | grep -z -v -i '\.pdf$' || true    # Modified non-PDF files
		git diff --name-only -z --diff-filter=D                                      # Deleted files of any type
	} | sort -z -u | sed -z -n "1,${maximum_files}p"                              # Remove duplicates and keep only as many as fit (sed reads ALL input, so sort never gets a broken pipe)
}                                                                              # End of list_other_changed_files

# ---------------- Phase 1: PDF Compression ----------------

function exclude_oversized_pdf() {                                                         # Tell Git (locally) to ignore a PDF that GitHub would reject
	local pdf_path="$1"                                                                       # The oversized PDF
	local escaped_path                                                                        # PDF path with glob characters escaped
	escaped_path=$(printf '%s' "$pdf_path" | sed -e 's/[][\\*?]/\\&/g')                       # Escape characters that gitignore treats as wildcards
	add_local_ignore_rule "/${escaped_path}"                                                  # Add an anchored ignore rule (the file stays on disk)
	TOTAL_PDFS_OVERSIZED=$((TOTAL_PDFS_OVERSIZED + 1))                                        # Count the oversized file
	log_message WARN "'${pdf_path}' exceeds the GitHub size limit and is now ignored by Git." # Tell the operator what happened
}                                                                                          # End of exclude_oversized_pdf

function compress_and_stage_pdf() {                                                                                              # Compress one PDF with Ghostscript and stage it
	local pdf_path="$1"                                                                                                             # The PDF to process
	local position_text="$2"                                                                                                        # Progress marker such as [3/57]
	local temporary_input="${WORK_DIRECTORY}/input.pdf"                                                                             # Private copy of the PDF that Ghostscript reads
	local temporary_output="${WORK_DIRECTORY}/output.pdf"                                                                           # Where Ghostscript writes the result
	local original_size_bytes                                                                                                       # Size before compression
	local compressed_size_bytes                                                                                                     # Size of Ghostscript's output
	local final_size_bytes                                                                                                          # Size of the file that will actually be committed
	local saved_percent                                                                                                             # How much smaller the file became
	local started_epoch                                                                                                             # When this PDF started
	local elapsed_seconds                                                                                                           # How long this PDF took
	local ghostscript_options=(-sDEVICE=pdfwrite -dCompatibilityLevel=1.4 "-dPDFSETTINGS=${GHOSTSCRIPT_QUALITY}" -dNOPAUSE -dBATCH) # Core Ghostscript options
	if [[ $LOG_LEVEL_NUMBER -gt 1 ]]; then                                                                                          # Only be chatty when DEBUG or TRACE logging is on
		ghostscript_options+=(-dQUIET)                                                                                                 # Silence Ghostscript's per-page output
	fi                                                                                                                              # End of the verbosity check

	original_size_bytes=$(stat -c %s -- "$pdf_path")                                                       # Record the original size in bytes
	started_epoch=$(date +%s)                                                                              # Record the start time
	log_message INFO "COMPRESSING ${position_text} '${pdf_path}' ($(format_bytes "$original_size_bytes"))" # Show which file is being compressed right now
	cp -- "$pdf_path" "$temporary_input"                                                                   # Copy the PDF into the temporary directory
	rm -f -- "$temporary_output"                                                                           # Remove any leftover output from a previous file

	if ! run_logged_command "ghostscript ${pdf_path}" "$GHOSTSCRIPT_TIMEOUT_SECONDS" gs "${ghostscript_options[@]}" -sOutputFile="$temporary_output" "$temporary_input"; then # Run Ghostscript; every output line is labelled with the PDF name
		fail_and_exit "Ghostscript failed or timed out for '${pdf_path}'."                                                                                                       # Stop on any compression failure
	fi                                                                                                                                                                        # End of the Ghostscript check
	if [[ ! -s "$temporary_output" ]]; then                                                                                                                                   # Ghostscript "succeeded" but wrote nothing
		fail_and_exit "Ghostscript produced an empty file for '${pdf_path}'."                                                                                                    # Stop to avoid data loss
	fi                                                                                                                                                                        # End of the empty-output check

	compressed_size_bytes=$(stat -c %s -- "$temporary_output")                                                                                                 # Measure Ghostscript's output
	if [[ $compressed_size_bytes -lt $original_size_bytes ]]; then                                                                                             # Only use the output if it is actually smaller
		mv -- "$temporary_output" "$pdf_path"                                                                                                                     # Replace the original with the compressed version
		chmod 644 -- "$pdf_path"                                                                                                                                  # mktemp files are 0600; restore normal permissions
		saved_percent=$(((original_size_bytes - compressed_size_bytes) * 100 / original_size_bytes))                                                              # Work out the saving (original is non-zero here)
		TOTAL_PDFS_COMPRESSED=$((TOTAL_PDFS_COMPRESSED + 1))                                                                                                      # Count the compressed PDF
		TOTAL_BYTES_BEFORE=$((TOTAL_BYTES_BEFORE + original_size_bytes))                                                                                          # Add to the "before" total
		TOTAL_BYTES_AFTER=$((TOTAL_BYTES_AFTER + compressed_size_bytes))                                                                                          # Add to the "after" total
		log_message INFO "COMPRESSED '${pdf_path}': $(format_bytes "$original_size_bytes") -> $(format_bytes "$compressed_size_bytes") (saved ${saved_percent}%)" # Log before and after sizes
	else                                                                                                                                                       # Compression did not help
		TOTAL_PDFS_KEPT_ORIGINAL=$((TOTAL_PDFS_KEPT_ORIGINAL + 1))                                                                                                # Count the unchanged PDF
		TOTAL_BYTES_BEFORE=$((TOTAL_BYTES_BEFORE + original_size_bytes))                                                                                          # Add the original to the "before" total
		TOTAL_BYTES_AFTER=$((TOTAL_BYTES_AFTER + original_size_bytes))                                                                                            # The file is unchanged, so "after" equals "before"
		log_message INFO "KEPT ORIGINAL '${pdf_path}': compressed copy was not smaller (${compressed_size_bytes} >= ${original_size_bytes} bytes)"                # Log why nothing changed
	fi                                                                                                                                                         # End of the size comparison
	rm -f -- "$temporary_input" "$temporary_output"                                                                                                            # Remove both temporary files
	elapsed_seconds=$(($(date +%s) - started_epoch))                                                                                                           # Measure how long this PDF took
	log_message DEBUG "Finished '${pdf_path}' in ${elapsed_seconds}s"                                                                                          # Log the duration

	final_size_bytes=$(stat -c %s -- "$pdf_path")               # Measure the file that would be committed
	if [[ $final_size_bytes -gt $GITHUB_MAX_FILE_BYTES ]]; then # GitHub would reject this file and break every push
		exclude_oversized_pdf "$pdf_path"                          # Ignore it locally instead of staging it
		return 0                                                   # Move on to the next PDF
	fi                                                          # End of the oversize check

	if ! run_logged_command "git-add" "$GIT_LOCAL_TIMEOUT_SECONDS" git add --verbose -- "$pdf_path"; then                         # Stage the PDF so it counts as processed
		fail_and_exit "Failed to stage '${pdf_path}'."                                                                               # Stop to avoid recompressing the same file forever
	fi                                                                                                                            # End of the staging check
	STAGED_BYTES=$((STAGED_BYTES + final_size_bytes))                                                                             # Keep the running byte total up to date within this cycle
	log_message DEBUG "Staged '${pdf_path}'. Batch so far: $(format_bytes "$STAGED_BYTES") of $(format_bytes "$MAX_BATCH_BYTES")" # Show how full the batch is
}                                                                                                                              # End of compress_and_stage_pdf

function compress_pending_pdfs() {                                                                      # Compress and stage as many pending PDFs as fit in the batch
	local staged_file_count                                                                                # Files already staged
	local room_in_batch                                                                                    # How many more files may be staged
	local pdf_path                                                                                         # Loop variable for each PDF
	local pdf_index=0                                                                                      # Position of the current PDF in this cycle
	local pdf_total                                                                                        # Number of PDFs selected in this cycle
	local pending_pdfs=()                                                                                  # Array holding the PDFs selected this cycle
	staged_file_count=$(count_staged_files)                                                                # Count what is already staged
	room_in_batch=$((MAX_BATCH_SIZE - staged_file_count))                                                  # Compute the remaining room in this batch
	if [[ $room_in_batch -le 0 ]]; then                                                                    # Batch is already full
		log_message DEBUG "Batch is full (${staged_file_count}/${MAX_BATCH_SIZE}); not compressing more yet." # Explain why nothing is compressed
		return 0                                                                                              # Nothing to do this cycle
	fi                                                                                                     # End of the room check

	mapfile -d '' -t pending_pdfs < <(list_pdfs_to_compress "$room_in_batch" || true)                                            # Load the smallest unstaged PDFs (NUL-safe) into the array
	pdf_total=${#pending_pdfs[@]}                                                                                                # Remember how many were selected
	if [[ $pdf_total -eq 0 ]]; then                                                                                              # No PDFs waiting
		log_message TRACE "No unstaged PDFs found this cycle."                                                                      # Log the empty result
		return 0                                                                                                                    # Nothing to do this cycle
	fi                                                                                                                           # End of the empty check
	log_message INFO "Found ${pdf_total} unstaged PDF(s) to compress (smallest first; room for ${room_in_batch} in this batch)." # Log how many were found
	verify_free_disk_space                                                                                                       # Make sure there is disk space before doing heavy work
	STAGED_BYTES=$(count_staged_bytes)                                                                                           # Start from the real number of staged bytes

	for pdf_path in "${pending_pdfs[@]}"; do                                                                                        # Process each PDF in order
		pdf_index=$((pdf_index + 1))                                                                                                   # Move the progress counter forward
		if [[ $STAGED_BYTES -ge $MAX_BATCH_BYTES ]]; then                                                                              # The batch is big enough for one push
			log_message INFO "Batch size limit reached (${STAGED_BYTES} >= ${MAX_BATCH_BYTES} bytes); the rest waits for the next batch." # Explain why we stop
			break                                                                                                                         # Stop compressing so the batch can be pushed
		fi                                                                                                                             # End of the batch-bytes check
		if [[ ! -f "$pdf_path" ]]; then                                                                                                # The file vanished after it was listed
			log_message DEBUG "'${pdf_path}' no longer exists; skipping."                                                                 # Log the skip
			continue                                                                                                                      # Go to the next PDF
		fi                                                                                                                             # End of the existence check
		compress_and_stage_pdf "$pdf_path" "[${pdf_index}/${pdf_total}]"                                                               # Compress the PDF and stage it
	done                                                                                                                            # End of the PDF loop
}                                                                                                                                # End of compress_pending_pdfs

# ---------------- Phase 2: Batch Push ----------------

function refresh_batch_counters() {                                                                                                                                                                    # Update the counters used by the push decision
	STAGED_FILE_COUNT=$(count_staged_files)                                                                                                                                                               # Number of files staged right now
	STAGED_BYTES=$(count_staged_bytes)                                                                                                                                                                    # Total bytes staged right now
	SECONDS_SINCE_LAST_PUSH=$(($(date +%s) - LAST_PUSH_EPOCH))                                                                                                                                            # Seconds since the last push
	log_message TRACE "Status: staged ${STAGED_FILE_COUNT}/${MAX_BATCH_SIZE} files, ${STAGED_BYTES}/${MAX_BATCH_BYTES} bytes, ${SECONDS_SINCE_LAST_PUSH}s/${MAX_SECONDS_BETWEEN_PUSHES}s since last push" # Log the counters
}                                                                                                                                                                                                      # End of refresh_batch_counters

function batch_is_ready() {                                                                                            # Succeeds (returns 0) when a push should happen now
	if [[ $STAGED_FILE_COUNT -ge $MAX_BATCH_SIZE ]]; then                                                                 # A full batch is staged
		log_message INFO "Push trigger: ${STAGED_FILE_COUNT} files staged (limit ${MAX_BATCH_SIZE})."                        # Log the reason
		return 0                                                                                                             # Ready to push
	fi                                                                                                                    # End of the batch-size check
	if [[ $STAGED_BYTES -ge $MAX_BATCH_BYTES ]]; then                                                                     # The staged files are big enough for one push
		log_message INFO "Push trigger: ${STAGED_BYTES} bytes staged (limit ${MAX_BATCH_BYTES})."                            # Log the reason
		return 0                                                                                                             # Ready to push
	fi                                                                                                                    # End of the batch-bytes check
	if [[ $SECONDS_SINCE_LAST_PUSH -ge $MAX_SECONDS_BETWEEN_PUSHES ]]; then                                               # The maximum wait has passed
		log_message INFO "Push trigger: ${SECONDS_SINCE_LAST_PUSH}s since last push (limit ${MAX_SECONDS_BETWEEN_PUSHES}s)." # Log the reason
		return 0                                                                                                             # Ready to push
	fi                                                                                                                    # End of the timer check
	return 1                                                                                                              # Not ready yet
}                                                                                                                      # End of batch_is_ready

function stage_other_changed_files() {                 # Fill leftover batch room with non-PDF changes and deletions
	local staged_file_count                               # Files already staged
	local room_in_batch                                   # How many more files may be staged
	local other_files=()                                  # Array holding the extra paths
	staged_file_count=$(count_staged_files)               # Count what is already staged
	room_in_batch=$((MAX_BATCH_SIZE - staged_file_count)) # Compute the remaining room
	if [[ $room_in_batch -le 0 ]]; then                   # No room left
		return 0                                             # Nothing to add
	fi                                                    # End of the room check

	mapfile -d '' -t other_files < <(list_other_changed_files "$room_in_batch" || true) # Load the extra paths (NUL-safe) into the array
	if [[ ${#other_files[@]} -eq 0 ]]; then                                             # Nothing extra to stage
		log_message DEBUG "No additional non-PDF changes to stage."                        # Log the empty result
		return 0                                                                           # Nothing to add
	fi                                                                                  # End of the empty check

	log_message INFO "Staging ${#other_files[@]} additional non-PDF change(s)..."                                                          # Log how many are being added
	if ! printf '%s\0' "${other_files[@]}" | run_logged_command "git-add" "$GIT_LOCAL_TIMEOUT_SECONDS" xargs -0 git add --verbose --; then # Stage them all, safely handling odd filenames
		fail_and_exit "Failed to stage additional files."                                                                                     # Stop on staging failure
	fi                                                                                                                                     # End of the staging check
}                                                                                                                                       # End of stage_other_changed_files

function commit_staged_batch() {                                                                         # Commit everything that is staged
	local staged_file_count                                                                                 # Number of files in this commit
	local commit_timestamp                                                                                  # UTC time for the commit message
	local commit_message                                                                                    # Full commit message
	staged_file_count=$(count_staged_files)                                                                 # Count the staged files
	commit_timestamp=$(date -u +'%Y-%m-%d %H:%M:%S UTC')                                                    # Build a UTC timestamp
	commit_message="Auto-sync commit (${staged_file_count} files) - ${commit_timestamp}"                    # Build the commit message
	log_message INFO "Committing ${staged_file_count} file(s): '${commit_message}'"                         # Log what is being committed
	git diff --cached --name-status --no-renames | relay_output_to_log DEBUG "commit-file"                  # List every file in the commit with its status letter (A/M/D)
	if ! run_logged_command "git-commit" "$GIT_LOCAL_TIMEOUT_SECONDS" git commit -m "$commit_message"; then # Create the commit
		fail_and_exit "Git commit failed."                                                                     # Stop on commit failure
	fi                                                                                                      # End of the commit check
	log_message INFO "Commit created: $(git rev-parse --short HEAD)"                                        # Log the new commit's short hash
}                                                                                                        # End of commit_staged_batch

function pull_remote_changes() {                                                                                            # Rebase our new commit on top of the remote branch
	log_message INFO "Pulling remote changes (rebase + autostash)..."                                                          # Log the start of the pull
	if ! run_logged_command "git-pull" "$GIT_NETWORK_TIMEOUT_SECONDS" git pull --verbose --progress --rebase --autostash; then # Pull; autostash protects unstaged work
		log_message WARN "Pull failed; aborting any half-finished rebase so the repository is left clean."                        # Explain the cleanup
		git rebase --abort || true                                                                                                # Abort a rebase in progress (harmless if none exists)
		fail_and_exit "Failed to pull/rebase from the remote repository."                                                         # Stop on pull failure
	fi                                                                                                                         # End of the pull check
	log_message DEBUG "Pull completed (or already up to date)."                                                                # Log success
}                                                                                                                           # End of pull_remote_changes

function push_to_remote() {                                                                            # Push the committed batch to GitHub
	log_message INFO "Pushing to remote repository..."                                                    # Log the start of the push
	if ! run_logged_command "git-push" "$GIT_NETWORK_TIMEOUT_SECONDS" git push --verbose --progress; then # Push the commit(s)
		fail_and_exit "Git push failed (network, permission, or file-size problem)."                         # Stop on push failure
	fi                                                                                                    # End of the push check
}                                                                                                      # End of push_to_remote

function push_batch() {                                                                                                                                                                               # Run the full stage, commit, pull, push sequence
	local staged_file_count                                                                                                                                                                              # Files in the batch after filling it
	local batch_bytes                                                                                                                                                                                    # Bytes in the batch
	local started_epoch                                                                                                                                                                                  # When this push sequence started
	local elapsed_seconds                                                                                                                                                                                # How long the sequence took
	local bytes_per_second                                                                                                                                                                               # Average throughput
	stage_other_changed_files                                                                                                                                                                            # Top up the batch with non-PDF changes if there is room
	staged_file_count=$(count_staged_files)                                                                                                                                                              # Count the final batch
	if [[ $staged_file_count -eq 0 ]]; then                                                                                                                                                              # Timer fired but there is nothing to push
		log_message INFO "Nothing staged; resetting the push timer."                                                                                                                                        # Explain the reset
		LAST_PUSH_EPOCH=$(date +%s)                                                                                                                                                                         # Restart the timer so we do not retry every cycle
		return 0                                                                                                                                                                                            # Done for this cycle
	fi                                                                                                                                                                                                   # End of the empty-batch check
	batch_bytes=$(count_staged_bytes)                                                                                                                                                                    # Measure the batch before committing
	started_epoch=$(date +%s)                                                                                                                                                                            # Record the start time
	log_message INFO "PUSH STARTING: ${staged_file_count} file(s), $(format_bytes "$batch_bytes")"                                                                                                       # Announce the push
	commit_staged_batch                                                                                                                                                                                  # Commit the batch
	pull_remote_changes                                                                                                                                                                                  # Rebase onto the latest remote state
	push_to_remote                                                                                                                                                                                       # Push the batch
	elapsed_seconds=$(($(date +%s) - started_epoch))                                                                                                                                                     # Measure the whole sequence
	bytes_per_second=$((batch_bytes / (elapsed_seconds > 0 ? elapsed_seconds : 1)))                                                                                                                      # Average throughput (avoid dividing by zero)
	TOTAL_PUSHES=$((TOTAL_PUSHES + 1))                                                                                                                                                                   # Count the push
	TOTAL_FILES_PUSHED=$((TOTAL_FILES_PUSHED + staged_file_count))                                                                                                                                       # Add the files to the total
	TOTAL_BYTES_PUSHED=$((TOTAL_BYTES_PUSHED + batch_bytes))                                                                                                                                             # Add the bytes to the total
	LAST_PUSH_EPOCH=$(date +%s)                                                                                                                                                                          # Restart the timer from the moment of the successful push
	log_message SUCCESS "PUSH COMPLETE: ${staged_file_count} file(s), $(format_bytes "$batch_bytes") in ${elapsed_seconds}s ($(format_bytes "$bytes_per_second")/s), HEAD $(git rev-parse --short HEAD)" # Report the result
	log_statistics                                                                                                                                                                                       # Show the updated totals
}                                                                                                                                                                                                     # End of push_batch

# ---------------- Main ----------------

function main() {                                             # Program entry point
	log_message INFO "Auto git sync ${SCRIPT_VERSION} starting." # Log startup (console only)
	verify_dependencies                                          # Make sure every tool exists
	acquire_single_instance_lock                                 # Prevent duplicate instances
	enter_repository_root                                        # Move into the repository
	verify_remote_access                                         # Prove that pushing can work before doing any heavy work
	prepare_work_directory                                       # Create the temporary directory
	add_local_ignore_rule "*.part"                               # Never commit the downloader's half-finished *.pdf.part files
	start_following_extra_logs                                   # Merge other apps' logs into this output, if configured
	log_environment_report                                       # Log versions, settings and the starting state
	verify_free_disk_space                                       # Check disk space once before starting
	STARTED_EPOCH=$(date +%s)                                    # Record when the main loop starts
	LAST_PUSH_EPOCH=$STARTED_EPOCH                               # Start the push timer now
	LAST_HEARTBEAT_EPOCH=$STARTED_EPOCH                          # Start the heartbeat timer now

	while true; do                                                                       # Monitor the repository forever
		CYCLE_NUMBER=$((CYCLE_NUMBER + 1))                                                  # Count this cycle
		log_message TRACE "------ cycle ${CYCLE_NUMBER} begins ------"                      # Mark the start of the cycle in the output
		compress_pending_pdfs                                                               # Phase 1: compress and stage PDFs
		refresh_batch_counters                                                              # Update staged count and elapsed time
		if batch_is_ready; then                                                             # Phase 2: decide whether to push
			push_batch                                                                         # Commit, pull and push the batch
		fi                                                                                  # End of the push decision
		log_heartbeat_if_due                                                                # Log a status line once per heartbeat interval
		log_message TRACE "cycle ${CYCLE_NUMBER} done; sleeping ${CHECK_INTERVAL_SECONDS}s" # Log the pause
		sleep "${CHECK_INTERVAL_SECONDS}" &                                                 # Sleep in the background so a stop signal is handled immediately
		wait "$!" || true                                                                   # Wait for the sleep to finish (a signal interrupts this wait at once)
	done                                                                                 # End of the monitoring loop
}                                                                                     # End of main

main "$@" # Start the script
