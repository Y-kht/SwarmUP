# Reads the lines a journal got since the last poll of the miner: only whole lines (a line being written is read at the next poll), at most
# a few megabytes each time, from where the last poll stopped. A journal that became shorter, or another file under the same name, is read
# again from its start.
import os


POLL_BYTES = 4 * 1024 * 1024


# It gives ([(offset of the line, its text)], the offset after them, the inode, the size, whether the file was read again from its start).
def readNew(path, offset=0, inode=0, limit=POLL_BYTES):
    try:
        status = os.stat(path)
    except OSError:
        return [], offset, inode, 0, False
    restarted = False
    if (inode and status.st_ino != inode) or status.st_size < offset:
        offset, restarted = 0, True
    if status.st_size == offset:
        return [], offset, status.st_ino, status.st_size, restarted
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(limit)
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) >= limit:
            return [], offset + len(data), status.st_ino, status.st_size, restarted
        return [], offset, status.st_ino, status.st_size, restarted
    lines, position = [], offset
    for raw in data[:end + 1].split(b"\n")[:-1]:
        lines.append((position, raw.decode("utf-8", "replace")))
        position += len(raw) + 1
    return lines, offset + end + 1, status.st_ino, status.st_size, restarted
