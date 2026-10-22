# What the models write, for the miner of patterns: the reasoning an agent writes before it acts is the costly part of a step, and the same
# reasoning written again and again (where is the button of the login page, which test command this project uses) is what a wheel saves.
# Two texts are near the same when they share most of their word triples (shingles). A bottom-k MinHash sketch of each text gives that
# share without keeping the texts: the k smallest hashes of its shingles, compared with those of another text. The smallest hashes are also
# its anchors, through which the store finds the texts that may be near it.
import hashlib
import re

from pattern_shapes import maskText


SKETCH = 48
ANCHORS = 4
MIN_WORDS = 12
CHUNK_WORDS = 400
SMALL_CHUNK = 40
WORD = re.compile(r"[a-z<>#][a-z0-9_<>#'-]*")
INTENT = re.compile(r"(?:^|[.!?\n]\s*)((?:i need to|i'll|i will|let me|let's|first,|next,|now i|i should|i must|we need to|to do this|my plan|step \d+[:.)])[^.!?\n]{3,160})",
                    re.IGNORECASE)
LOCATION = re.compile(r"\b(button|link|field|input|icon|menu|tab|checkbox|box|element|image|logo|title|header|search bar|text box)\b[^.\n]{0,80}?"
                      r"(?:at|around|near|coordinates?|position)\s*\(?\s*(-?\d{1,5})\s*[,x]\s*(-?\d{1,5})\s*\)?", re.IGNORECASE)
FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
CODE_LINE = re.compile(r"(^\s*(import|from|def|class|func|let|var|const|public|private|static|return|if|for|while|guard|case|struct|enum|#include|fn|use)\b)|"
                       r"([{};]\s*$)|(^\s*[}\])]\s*$)|(^\s*(//|#|/\*|\*))")
CODE_SHARE = 0.35
STOP_WORDS = frozenset("a an the of to in on at for with and or is are was were be been it this that these those i we you he she they them its "
                       "my our your as by from so if then than but not no do does did can could will would should shall may might must have has had "
                       "there here which what who whom when where why how all any each".split())


def normalize(text):
    return maskText(str(text or "").lower())


def wordsOf(text):
    return [word for word in WORD.findall(normalize(text)) if word not in STOP_WORDS]


def shingleHash(words):
    return int.from_bytes(hashlib.blake2b(" ".join(words).encode("utf-8"), digest_size=8).digest(), "big")


# The sketch of a text: the SKETCH smallest hashes of its word triples, sorted. A short text gives an empty sketch.
def sketchOf(text, words=None):
    words = wordsOf(text) if words is None else words
    if len(words) < MIN_WORDS:
        return []
    hashes = {shingleHash(words[index:index + 3]) for index in range(len(words) - 2)}
    return sorted(hashes)[:SKETCH]


def anchorsOf(sketch):
    return sketch[:ANCHORS]


# How near two sketches are (an estimate of the Jaccard similarity of the two sets of triples).
def similarity(first, second):
    if not first or not second:
        return 0.0
    union = sorted(set(first) | set(second))[:SKETCH]
    both = set(first) & set(second)
    return sum(1 for value in union if value in both) / len(union)


# Code is what the model makes, not how it reasons: blocks of code, and paragraphs that are mostly code, are left out.
def looksLikeCode(paragraph):
    lines = [line for line in paragraph.splitlines() if line.strip()]
    return bool(lines) and sum(1 for line in lines if CODE_LINE.search(line)) >= CODE_SHARE * len(lines) and len(lines) >= 2 or \
        len(re.findall(r"[{}();=<>\[\]]", paragraph)) > 0.06 * max(len(paragraph), 1)


# The parts of a text worth comparing: its paragraphs without code, small ones joined, long ones cut.
def chunksOf(text):
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", FENCE.sub("\n\n", str(text or ""))) if part.strip() and not looksLikeCode(part)]
    chunks, current = [], ""
    for paragraph in paragraphs:
        current = f"{current}\n\n{paragraph}" if current else paragraph
        if len(current.split()) >= SMALL_CHUNK:
            chunks.append(current)
            current = ""
    if current:
        if chunks and len(current.split()) < MIN_WORDS:
            chunks[-1] = f"{chunks[-1]}\n\n{current}"
        else:
            chunks.append(current)
    cut = []
    for chunk in chunks:
        words = chunk.split()
        cut += [" ".join(words[start:start + CHUNK_WORDS]) for start in range(0, len(words), CHUNK_WORDS)]
    return [chunk for chunk in cut if len(chunk.split()) >= MIN_WORDS]


# What the model says it sets out to do ("I need to find the login button"), as short masked templates.
def intentsOf(text):
    found = []
    for match in INTENT.finditer(str(text or "")):
        words = normalize(match.group(1)).split()[:10]
        if len(words) >= 4:
            found.append(" ".join(words))
    return found[:6]


# Where the model says a thing of a screen is: [(thing, x, y)].
def locationsOf(text):
    return [(match.group(1).lower(), int(match.group(2)), int(match.group(3))) for match in LOCATION.finditer(str(text or ""))][:6]


def labelOf(text, words=24):
    return " ".join(str(text or "").split()[:words])
