# ───────────────────────────── smart moment picking and post text (pure functions) ─────────────────────────────
# ───────────────────── Hinglish (Hindi written in English letters) ─────────────────────
# Whisper has no "Hinglish" language. Clipper transcribes it as Hindi, nudges the model to write English letters with a
# Roman-script prompt, and converts any Devanagari that still comes out. English words come out of Whisper as normal letters.
import itertools, math, re
from collections import Counter
from clipper.sysutil import DEFAULT_FONT
from clipper.edit import _num, ass_text
HINGLISH_NAMES = {"hinglish", "hi-latn", "hi-en", "hi_en", "hindi-english", "hien"}
HINGLISH_PROMPT = ("Aaj hum is video mein baat karenge. Haan bhai, yeh kaafi mazedaar hai, toh chalo shuru karte hain. "
                   "Matlab dekho, mujhe lagta hai ki sab log isse pasand karenge, kyunki yeh bilkul alag hai.")

def is_hinglish(lang):
    return str(lang or "").strip().lower().replace(" ", "") in HINGLISH_NAMES

def hinglish_note(c):
    return " The video is in Hinglish (Hindi written in English letters). Write the title, hook and caption in the same natural Hinglish style, never in Devanagari." if is_hinglish(c.get("language")) else ""

_DEV_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
             "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l",
             "ळ": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h"}
_DEV_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f"}
_DEV_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_DEV_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
_DEV_PUNCT = str.maketrans({"।": ".", "॥": ".", **{chr(0x966 + i): str(i) for i in range(10)}})
# Words whose usual Hinglish spelling differs from a letter-by-letter conversion: common Hindi words and English words Whisper writes in Devanagari.
_DEV_WORDS = {w: r for r, ws in {
    "mein": "में", "main": "मैं", "hai": "है", "hain": "हैं", "nahi": "नहीं नही", "ki": "कि की", "ka": "का", "ke": "के", "ko": "को", "se": "से", "aur": "और", "yeh": "यह",
    "ye": "ये", "woh": "वो वह", "kya": "क्या", "kuch": "कुछ", "toh": "तो", "bhi": "भी", "hi": "ही", "hum": "हम", "aap": "आप", "tum": "तुम", "mujhe": "मुझे", "tujhe": "तुझे",
    "hamein": "हमें", "use": "उसे", "isse": "इसे इससे", "kaise": "कैसे", "kyun": "क्यों", "kahan": "कहाँ कहां", "yahan": "यहाँ यहां", "wahan": "वहाँ वहां", "jahan": "जहाँ जहां",
    "kab": "कब", "jab": "जब", "tab": "तब", "ab": "अब", "bahut": "बहुत", "accha": "अच्छा", "theek": "ठीक", "lekin": "लेकिन", "par": "पर", "tha": "था", "thi": "थी", "the": "थे",
    "ho": "हो", "hoon": "हूँ हूं", "raha": "रहा", "rahi": "रही", "rahe": "रहे", "kar": "कर", "karo": "करो", "karna": "करना", "sab": "सब", "koi": "कोई", "ek": "एक", "do": "दो",
    "bhai": "भाई", "yaar": "यार", "arre": "अरे", "okay": "ओके ओके।", "haan": "हाँ हां", "na": "ना", "ji": "जी", "matlab": "मतलब", "bilkul": "बिल्कुल", "aaj": "आज", "kal": "कल",
    "video": "वीडियो विडियो", "videos": "वीडियोज", "audio": "ऑडियो", "phone": "फोन फ़ोन", "mobile": "मोबाइल", "computer": "कंप्यूटर कम्प्यूटर", "internet": "इंटरनेट",
    "youtube": "यूट्यूब यूट्यूब", "channel": "चैनल", "subscribe": "सब्सक्राइब", "like": "लाइक", "share": "शेयर", "comment": "कमेंट कमेन्ट", "reel": "रील", "reels": "रील्स",
    "story": "स्टोरी", "post": "पोस्ट", "online": "ऑनलाइन", "offline": "ऑफलाइन", "game": "गेम", "gaming": "गेमिंग", "gameplay": "गेमप्ले", "live": "लाइव", "stream": "स्ट्रीम",
    "top": "टॉप", "best": "बेस्ट", "life": "लाइफ", "time": "टाइम", "please": "प्लीज प्लीज़", "sorry": "सॉरी", "thank you": "थैंक्यू", "friend": "फ्रेंड", "friends": "फ्रेंड्स",
    "family": "फैमिली", "message": "मैसेज", "call": "कॉल", "number": "नंबर", "bank": "बैंक", "car": "कार", "bus": "बस", "train": "ट्रेन", "hotel": "होटल", "movie": "मूवी",
    "film": "फिल्म", "song": "सॉन्ग", "music": "म्यूजिक", "party": "पार्टी", "boss": "बॉस", "office": "ऑफिस", "job": "जॉब", "project": "प्रोजेक्ट", "problem": "प्रॉब्लम",
    "serious": "सीरियस", "start": "स्टार्ट", "stop": "स्टॉप", "school": "स्कूल", "college": "कॉलेज", "doctor": "डॉक्टर", "police": "पुलिस", "special": "स्पेशल", "level": "लेवल",
    "result": "रिजल्ट", "real": "रियल", "first": "फर्स्ट", "next": "नेक्स्ट", "full": "फुल", "free": "फ्री", "paise": "पैसे", "log": "लोग", "kam": "कम", "zyada": "ज़्यादा ज्यादा",
}.items() for w in ws.split()}

def _dev_word(w):
    """One Devanagari word to Hinglish: letter by letter, with the schwa dropped where Hindi drops it (kamla, not kamala)."""
    import unicodedata
    known = _DEV_WORDS.get(unicodedata.normalize("NFC", w))
    if known: return known
    w = unicodedata.normalize("NFD", w).replace("ज्ञ", "ग्य")
    units, i, n = [], 0, len(w)  # unit = [consonant, vowel, tail, kept_vowel]
    while i < n:
        ch = w[i]; i += 1
        if ch in _DEV_CONS:
            cons = _DEV_CONS[ch]
            if i < n and w[i] == "\u093c": cons = _DEV_NUKTA.get(ch, cons); i += 1
            if i < n and w[i] in _DEV_MATRAS: units.append([cons, _DEV_MATRAS[w[i]], "", False]); i += 1
            elif i < n and w[i] == "\u094d": units.append([cons, "", "", False]); i += 1
            else: units.append([cons, "a", "", True])  # the inherent "a" is the only vowel that may be dropped
        elif ch in _DEV_VOWELS: units.append(["", _DEV_VOWELS[ch], "", False])
        elif ch in "\u0902\u0901" and units: units[-1][2] += "n"
        elif ch == "\u0903" and units: units[-1][2] += "h"
        elif ch in _DEV_MATRAS and units and not units[-1][1]: units[-1][1] = _DEV_MATRAS[ch]
    if len(units) > 1 and units[-1][3] and not units[-1][2]: units[-1][1] = ""  # final "a" is silent: kamal
    for k in range(1, len(units) - 1):  # a middle "a" between two vowel sounds is silent: kamla, not kamala
        if units[k][3] and not units[k][2] and units[k - 1][1] and units[k + 1][1]: units[k][1] = ""
    for k, u in enumerate(units):
        if u[0] == "v" and k and not units[k - 1][1]: u[0] = "w"  # swagat, dwar
        if u[0] and u[1] == "e" and u[2].startswith("n"): u[1] = "ei"  # ismein, hamein
    last = units[-1] if units else None
    if last and last[0]:  # word endings are short in Hinglish: lagta, nahi, tu
        if last[1] == "ee": last[1] = "i"
        elif last[1] == "oo" and not last[2]: last[1] = "u"
        elif last[1] == "aa" and not last[2]: last[1] = "a"
    return "".join(c + v + t for c, v, t, _ in units)

def hinglish_text(text, sentence=False):
    """Convert any Devanagari in the text to Hinglish. Text already in English letters is returned unchanged.
    sentence=True also capitalises the first letter, for a whole caption line (not for single words)."""
    if not re.search(r"[\u0900-\u097F]", text): return text
    def run(m):
        pieces = re.split(r"([.0-9]+)", m.group(0).translate(_DEV_PUNCT))
        return "".join(p if re.fullmatch(r"[.0-9]*", p) else _dev_word(p) for p in pieces)
    out = re.sub(r"[\u0900-\u097F]+", run, text)
    if sentence: out = re.sub(r"(^\s*|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)  # capital letters after . ? !
    return out

STOP = frozenset("a an the and or but if of to in on at for with is are was were be been being it its this that these those i you he she we they me him her us them my your our "
                 "their so as by from not no do does did have has had will would can could just very really like um uh yeah okay oh well there here then than too also about into "
                 "out up down over one get got go going gonna know think mean say said".split())
STOP |= frozenset("hai hain tha thi the ho hoon toh ki ka ke ko se mein par pe ye yeh wo woh vo aur bhi hi nahi nahin na kya kuch koi hum tum aap main mai mera meri mere tera teri tere "
                  "uska uski uske isme usme jo jab tab ab phir fir ek bahut kar karo karna kiya hua hue raha rahi rahe lekin magar agar haan matlab bas bhai yaar".split())  # Hinglish
HOOK_WORDS = frozenset("secret secrets mistake mistakes truth never always nobody everyone everybody stop wrong worst best biggest hidden crazy insane shocking unbelievable actually reason "
                       "why how what imagine warning dangerous free easy fastest simple trick hack lesson changed regret wish scam lie lied exposed banned problem "
                       "raaz sach galti kabhi sabse khatarnak jhooth dhoka asli sacchai".split())
EMO_WORDS = frozenset("love hate amazing awesome incredible crazy insane wild scary terrifying funny hilarious laugh cry angry furious shocked shocking disgusting beautiful perfect terrible "
                      "horrible worst best unbelievable won lost died dead killed scared excited epic legend nightmare dream heartbreaking proud embarrassing wtf omg damn holy "
                      "pagal kamaal zabardast mast maza mazedaar dard khushi gussa darr badhiya ekdum".split())
WEAK_START = re.compile(r"^(and|but|so|um|uh|well|yeah|yes|no|okay|ok|anyway|because|which|then|also|or|like|right|hmm|oh)\b", re.I)
DANGLING = re.compile(r"^(it|that|this|they|he|she|them|those|these|there)\b", re.I)  # points back at something said earlier
GREETING = re.compile(r"\b(welcome back|what'?s up|hey guys|hi guys|hello everyone|hello guys|how'?s everybody|good morning|good evening|thanks for watching|thank you for watching|"
                      r"see you (in|on) the next|subscribe|smash that|hit the like|like and subscribe|before we (get|start|begin)|in today'?s (video|stream)|let'?s get into it)\b", re.I)
SPONSOR = re.compile(r"\b(sponsor(ed)?|promo code|discount code|use code|link in the (description|bio)|affiliate|brought to you by|patreon|merch)\b", re.I)
FILLERS = frozenset("um uh er ah so and but like okay ok yeah well right hmm".split())
SPAM_TAGS = frozenset("fyp foryou foryoupage viral trending explore follow like instagood reels shorts tiktok trend followme viralvideo".split())
YT_CATEGORIES = {"People & Blogs": "22", "Gaming": "20", "Entertainment": "24", "Comedy": "23", "Education": "27", "Howto & Style": "26", "Science & Technology": "28",
                 "Sports": "17", "News & Politics": "25", "Film & Animation": "1", "Music": "10", "Pets & Animals": "15", "Travel & Events": "19", "Autos & Vehicles": "2"}
RATE_KEYS = (("s_hook", 1.5), ("s_payoff", 1.2), ("s_alone", 1.2), ("s_emotion", 1.0), ("s_share", 1.0))
WHY = {"hook": "strong opening", "emo": "emotional language", "idf": "specific, information-dense", "wps": "good speaking pace", "dead": "little dead air",
       "quest": "asks a question", "len": "good length", "peaks": "loud, high-energy peaks", "loud": "energetic audio", "dyn": "dynamic delivery", "quiet": "no quiet stretches"}

def parse_loudness(lines):
    """RMS level in dB for each analysis frame, from ffmpeg's astats metadata output."""
    out = []
    for ln in lines:
        m = re.search(r"lavfi\.astats\.Overall\.RMS_level=(\S+)", ln)
        if m:
            try: v = float(m.group(1))
            except ValueError: v = -80.0
            out.append(max(-80.0, v) if math.isfinite(v) else -80.0)
    return out

def hook_score(text):
    """How strongly the opening of a moment grabs attention (higher is better; penalties are applied separately)."""
    t = text.strip(); ws = re.findall(r"[a-z']+", t.lower())
    s = 1.2 if "?" in t[:220] else 0.0
    s += 0.5 * min(3, sum(w in HOOK_WORDS for w in ws))
    s += 0.5 if re.search(r"\d", t) else 0.0
    s += 0.4 if re.search(r"\byou(r|'re|'ll)?\b", t, re.I) else 0.0
    s += 0.3 if "!" in t else 0.0
    s += 0.4 if len(ws) >= 8 else 0.0  # something is actually being said
    return s

def rank_windows(segs, total, min_len, max_len, loud=None, hop=0.5, relaxed=False):
    """Score every sentence-aligned window of the allowed length on transcript and audio signals.
    Returns dicts (i, j = first and last segment, start, end, raw, heur 0-10, why) sorted best first."""
    n = len(segs)
    if n == 0: return []
    texts = [str(s["text"]).strip() for s in segs]
    toks = [re.findall(r"[a-z0-9']+", t.lower()) for t in texts]
    df = Counter(w for ts in toks for w in set(ts) if w not in STOP and len(w) > 2)
    idf = {w: math.log((n + 1) / (k + 1)) + 1 for w, k in df.items()}
    cont = [[w for w in ts if w not in STOP and len(w) > 2] for ts in toks]
    ends = [bool(re.search(r"[.!?…][\"')\]]*$", t)) for t in texts]
    gap = [segs[k + 1]["start"] - segs[k]["end"] if k + 1 < n else 9.0 for k in range(n)]
    pre = lambda v: [0.0] + list(itertools.accumulate(v))  # prefix sums: any window total in O(1)
    P_wc, P_sp = pre(len(ts) for ts in toks), pre(max(0.0, s["end"] - s["start"]) for s in segs)
    P_idf, P_ct = pre(sum(idf[w] for w in ct) for ct in cont), pre(len(ct) for ct in cont)
    P_emo = pre(sum(w in EMO_WORDS for w in ts) + 0.5 * t.count("!") for ts, t in zip(toks, texts))
    P_q, P_bad = pre(min(2, t.count("?")) for t in texts), pre(1 if (GREETING.search(t) or SPONSOR.search(t)) else 0 for t in texts)
    hook = []
    for i in range(n):
        j, parts = i, []
        while j < n and len(parts) < 3 and segs[j]["start"] - segs[i]["start"] < 8: parts.append(texts[j]); j += 1
        hook.append(hook_score(" ".join(parts)))
    weak, dang = [bool(WEAK_START.match(t)) for t in texts], [bool(DANGLING.match(t)) for t in texts]
    if loud:
        med = sorted(loud)[len(loud) // 2]  # judge loudness against this video's own normal level
        P_db, P_db2 = pre(loud), pre(v * v for v in loud)
        P_pk, P_qt = pre(1 if v > med + 6 else 0 for v in loud), pre(1 if v < med - 15 else 0 for v in loud)
    rows = []
    for i in range(n):
        a = segs[i]["start"]
        if not relaxed and i > 0 and not (ends[i - 1] or gap[i - 1] >= 0.5): continue  # start where a sentence or pause begins
        for j in range(i, n):
            b = segs[j]["end"]; L = b - a
            if L > max_len: break
            if L < min_len: continue
            if not relaxed and not (ends[j] or gap[j] >= 0.6): continue  # end where a sentence or pause ends
            rows.append((i, j, a, b, L))
    if not rows: return []
    F = {k: [] for k in ("hook", "emo", "idf", "wps", "dead", "quest", "len", "peaks", "loud", "dyn", "quiet")}
    target = min_len + 0.35 * (max_len - min_len)
    for i, j, a, b, L in rows:
        words, sp = P_wc[j + 1] - P_wc[i], P_sp[j + 1] - P_sp[i]
        F["hook"].append(hook[i]); F["emo"].append((P_emo[j + 1] - P_emo[i]) / max(words, 1) * 100)
        F["idf"].append((P_idf[j + 1] - P_idf[i]) / max(P_ct[j + 1] - P_ct[i], 1))
        F["wps"].append(1 - min(1.0, abs(words / L - 2.7) / 2.7)); F["dead"].append(max(0.0, 1 - sp / L))
        F["quest"].append(P_q[j + 1] - P_q[i]); F["len"].append(-abs(L - target) / max(max_len - min_len, 1))
        if loud:
            fa = min(int(a / hop), len(loud) - 1); fb = min(len(loud), max(int(math.ceil(b / hop)), fa + 1)); nf = fb - fa
            mean = (P_db[fb] - P_db[fa]) / nf; var = max((P_db2[fb] - P_db2[fa]) / nf - mean * mean, 0.0)
            F["peaks"].append((P_pk[fb] - P_pk[fa]) / nf); F["loud"].append(mean); F["dyn"].append(math.sqrt(var)); F["quiet"].append((P_qt[fb] - P_qt[fa]) / nf)
    def z(v):
        m = sum(v) / len(v); sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) or 1.0
        return [max(-2.5, min(2.5, (x - m) / sd)) for x in v]
    W = {"hook": 1.5, "emo": 0.7, "idf": 0.5, "wps": 0.4, "dead": -0.8, "quest": 0.3, "len": 0.2, "peaks": 0.9, "loud": 0.4, "dyn": 0.3, "quiet": -0.4}
    Z = {k: z(v) for k, v in F.items() if v}
    wins = []
    for r, (i, j, a, b, L) in enumerate(rows):
        raw = sum(W[k] * Z[k][r] for k in Z)
        if P_bad[j + 1] - P_bad[i]: raw -= 3.0  # greeting, sponsor read or "subscribe" inside the clip
        raw -= 1.0 if weak[i] else (0.8 if dang[i] else 0.0)  # opens mid-thought
        raw += (0.6 if ends[j] else 0.0) + (0.3 if gap[j] >= 0.6 else 0.0)  # ends cleanly
        wins.append({"i": i, "j": j, "start": a, "end": b, "raw": raw, "heur": 10 / (1 + math.exp(-raw / 2.5)), "r": r})
    wins.sort(key=lambda w: -w["raw"])
    for w in wins[:2000]:
        contrib = sorted(((W[k] * Z[k][w["r"]], k) for k in Z), reverse=True)
        w["why"] = [WHY[k] for v, k in contrib[:3] if v > 0.3]
    return wins

def select_candidates(wins, k, max_overlap=0.25):
    """The k best windows that do not substantially overlap each other."""
    out = []
    for w in wins:
        if all(min(w["end"], o["end"]) - max(w["start"], o["start"]) <= max_overlap * min(w["end"] - w["start"], o["end"] - o["start"]) for o in out):
            out.append(w)
            if len(out) >= k: break
    return out

def refine_bounds(w, segs, min_len, total):
    """Final start and end of a chosen window: skip a few leading filler words, keep a little room around the speech."""
    i, j = w["i"], w["j"]; a, b = float(segs[i]["start"]), float(segs[j]["end"])
    words = segs[i].get("words") or []
    k = 0
    while (k < min(3, len(words) - 1) and re.sub(r"[^\w']", "", str(words[k].get("word", "")).lower()) in FILLERS
           and b - float(words[k + 1]["start"]) >= min_len):
        k += 1
    prev_end = float(segs[i - 1]["end"]) if i > 0 else 0.0
    if k: a = max(float(words[k - 1]["end"]), float(words[k]["start"]) - 0.12)
    else: a = max(prev_end, a - 0.15)
    last = segs[j].get("words") or []
    if last: b = max(b, float(last[-1]["end"]))
    nxt = float(segs[j + 1]["start"]) if j + 1 < len(segs) else total
    return max(0.0, a), min(total, b + 0.3, max(b, nxt - 0.02))

def clip_text(t, n=1400):
    return t if len(t) <= n else t[:n * 7 // 10].rstrip() + " ... " + t[-n * 27 // 100:].lstrip()

def rerank_prompt(items, c):
    """items: (id, seconds, text). Asks the model to rate each candidate and write its title, hook, caption, hashtags and keywords."""
    niche = (f" The channel's niche: {c['audience']}." if c["audience"] else "") + hinglish_note(c)
    focus = f" Prefer moments about: {c['focus']}." if c["focus"] else ""
    body = "\n\n".join(f"[id {i} | {sec:.0f}s]\n{txt}" for i, sec, txt in items)
    return ("You are an expert short-form video editor and growth strategist." + niche + focus + "\n"
            "Below are candidate moments from a longer video, each with an id and its transcript. Judge each one the way a viewer scrolling Reels, Shorts or TikTok would: "
            "would they stop, watch to the end, and share it?\n"
            "Rate every candidate from 1 to 10 on: s_hook (how strong the first 3 seconds are: curiosity, tension, a bold claim, a question), "
            "s_payoff (a complete, satisfying point or story by the end), s_alone (makes sense with no other context), "
            "s_emotion (funny, surprising, inspiring or intense), s_share (people would send it to a friend or comment).\n"
            "Be honest: most moments are average. Use the whole 1-10 range and give weak moments low scores.\n"
            "For every candidate also write:\n"
            "- title: at most 60 characters, starts with the most searchable keyword or the hook, specific, honest, no hashtags, no emojis\n"
            "- hook: on-screen text for the first 2 seconds, 3 to 6 words\n"
            "- caption: one or two short sentences. The first one is a hook. End with a question that invites comments. At most 220 characters, no hashtags\n"
            "- hashtags: 3 to 5 lowercase words without the # sign: one broad topic tag and two to four specific niche tags. No generic spam like fyp or viral\n"
            "- keywords: 4 to 8 phrases people would search to find this\n"
            "Only describe what is in the transcript; never invent facts. Write in the language of the transcript.\n"
            "Return valid JSON only: "
            '{"clips":[{"id":1,"s_hook":7,"s_payoff":8,"s_alone":9,"s_emotion":6,"s_share":7,"title":"...","hook":"...","caption":"...","hashtags":["..."],"keywords":["..."]}]}\n\n'
            "Candidates:\n" + body)

def parse_rating(item, valid_ids):
    """One rated candidate from the AI -> (id, score 1-10, metadata), or None when unusable."""
    if not isinstance(item, dict): return None
    try: cid = int(item.get("id"))
    except (TypeError, ValueError): return None
    if cid not in valid_ids: return None
    vals = [(_num(item.get(k), 1, 10), wgt) for k, wgt in RATE_KEYS]
    if all(v is None for v, _ in vals): return None
    score = sum((5.0 if v is None else v) * wgt for v, wgt in vals) / sum(wgt for _, wgt in vals)
    tags, kws = item.get("hashtags"), item.get("keywords")
    return cid, score, {"title": str(item.get("title") or ""), "hook": str(item.get("hook") or ""), "caption": str(item.get("caption") or ""),
                        "hashtags": [str(t) for t in tags] if isinstance(tags, list) else [], "keywords": [str(k) for k in kws] if isinstance(kws, list) else [],
                        "ratings": {k: v for (k, _), (v, _w) in zip(RATE_KEYS, vals) if v is not None}}

def clean_title(s, limit=60):
    if s is None: return ""
    s = re.sub(r"\s+", " ", re.sub(r"https?://\S+|#\w+", "", str(s))).strip(" \"'-–—:;,.")
    if len(s) > 6 and s.isupper(): s = s.capitalize()
    if len(s) > limit: s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-–—")
    return s

def clean_tags(tags, extra, n):
    """Lowercase, de-duplicated hashtag words. The user's own tags come first and are never filtered; generic spam tags are dropped."""
    out, seen = [], set()
    for t, own in [(t, True) for t in extra] + [(t, False) for t in tags or []]:
        t = re.sub(r"\W", "", str(t).lstrip("#").lower())
        if not (3 <= len(t) <= 30) or t in seen or t.isdigit() or (not own and t in SPAM_TAGS): continue
        seen.add(t); out.append(t)
    return out[:max(n, 1)]

def compose_caption(caption, tags, credit=""):
    parts = [str(caption).strip(), credit, " ".join("#" + t for t in tags)]
    return "\n\n".join(p for p in parts if p)

BORING_WORDS = STOP | frozenset("what when where which while would should because every thing things something anything people actually still even much many more most some such only other "
                                "been after before again never always around little being their them then told tell said says okay yeah right".split())

def keyword_idf(segs):
    """How rare each word is across the whole video (rare words make better hashtags than common ones)."""
    docs = [set(re.findall(r"[a-z]{4,}", str(s["text"]).lower())) for s in segs]
    df = Counter(w for d in docs for w in d)
    return {w: math.log((len(docs) + 1) / (k + 1)) + 1 for w, k in df.items()}

def fallback_meta(text, idf=None):
    """Title, caption and hashtags from the transcript alone, for when the AI is off or gave nothing usable."""
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0] if text.strip() else ""
    tf = Counter(w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in BORING_WORDS)
    words = [w for w, _ in sorted(tf.items(), key=lambda kv: -kv[1] * (idf or {}).get(kv[0], 1.0))]
    return {"title": clean_title(first) or "Highlight", "caption": clean_title(first, 200) or "Highlight", "hashtags": words[:4], "keywords": words[:6]}

def youtube_description(desc, src):
    """Caption + original link + hashtags with #Shorts first (YouTube shows the first three hashtags above the title)."""
    paras = [q.strip() for q in re.split(r"\n\s*\n", str(desc).strip()) if q.strip()]
    tag_i = next((k for k in range(len(paras) - 1, -1, -1) if re.fullmatch(r"(#\w+\s*)+", paras[k])), None)
    if tag_i is None: paras.append("#Shorts"); tag_i = len(paras) - 1
    elif "#shorts" not in paras[tag_i].lower(): paras[tag_i] = "#Shorts " + paras[tag_i]
    if str(src).startswith("http"): paras.insert(tag_i, "Original video: " + str(src))
    return "\n\n".join(paras)

def youtube_fields(title, description, keywords, src):
    desc = youtube_description(description, src)
    tags, size = [], 0
    for t in [str(k) for k in keywords or []] + re.findall(r"#(\w+)", desc):
        t = t.strip().lstrip("#")
        if t and t.lower() not in {x.lower() for x in tags} and size + len(t) + 1 <= 450: tags.append(t); size += len(t) + 1
    return {"title": re.sub(r"\s+", " ", str(title)).strip()[:100], "description": desc, "tags": tags}

RATING_NAMES = {"s_hook": "hook", "s_payoff": "payoff", "s_alone": "stands alone", "s_emotion": "emotion", "s_share": "shareability"}

def describe_score(cl):
    """Short text for the Review window: the clip's score, why it was picked and the AI's ratings. '' when the clip has no score."""
    score = cl.get("score")
    if score is None: return ""
    out = [f"Score {score}/10" + (": " + ", ".join(cl["why"]) if cl.get("why") else "")]
    ratings = cl.get("ratings") or {}
    if ratings: out.append("AI ratings: " + " · ".join(f"{RATING_NAMES.get(k, k)} {v:g}" for k, v in ratings.items()))
    return "\n".join(out)

def post_text(cl, yt):
    out = [f"TITLE\n{cl['title']}", f"CAPTION (Instagram Reels / TikTok)\n{cl['description']}",
           f"YOUTUBE SHORTS\nTitle: {yt['title']}\nTags: {', '.join(yt['tags'])}\nDescription:\n{yt['description']}"]
    if cl.get("hook"): out.append(f"ON-SCREEN HOOK\n{cl['hook']}")
    if cl.get("score") is not None: out.append(f"WHY THIS MOMENT\nScore {cl['score']}/10" + (": " + ", ".join(cl["why"]) if cl.get("why") else ""))
    return "\n\n".join(out) + "\n"

def exclude_cam(main, cam, mode):
    """Keep the face cam out of the content panel when the cam box sits on top of the content box.
    main and cam are (x, y, w, h) in the same pixel space. Returns (content crop, patch, how):
    'crop' = the biggest part of the content box that doesn't touch the cam,
    'fill' = the whole content box with the cam area (patch, relative to the crop) painted over,
    'none' = no overlap, or mode 'keep'. Mode 'auto' crops when that keeps at least half of the content, else fills."""
    mx, my, mw, mh = main; cx, cy, cw, ch = cam
    pad = max(2, round(mw * 0.012))  # a little margin around the cam so no edge of the face is left
    ox0, oy0, ox1, oy1 = max(mx, cx - pad), max(my, cy - pad), min(mx + mw, cx + cw + pad), min(my + mh, cy + ch + pad)
    if mode == "keep" or ox0 >= ox1 or oy0 >= oy1: return main, None, "none"
    def usable(r):
        x, y, w, h = r; w, h = w // 2 * 2, h // 2 * 2
        return (x, y, w, h) if w >= 16 and h >= 16 else None
    cands = [usable(r) for r in ((mx, my, mw, oy0 - my), (mx, oy1, mw, my + mh - oy1), (mx, my, ox0 - mx, mh), (ox1, my, mx + mw - ox1, mh))]
    best = max((r for r in cands if r), key=lambda r: r[2] * r[3], default=None)
    if mode != "fill" and best and (mode == "crop" or best[2] * best[3] >= 0.5 * mw * mh): return best, None, "crop"
    px, py = max(1, ox0 - mx), max(1, oy0 - my)
    pw, ph = min(ox1 - mx, mw - 1) - px, min(oy1 - my, mh - 1) - py
    return (main, (px, py, pw, ph), "fill") if pw >= 2 and ph >= 2 else (main, None, "none")

def parse_region(s):
    """'x,y,w,h' as fractions of the video -> tuple, or None when missing or invalid."""
    try: v = [float(x) for x in str(s).split(",")]
    except ValueError: return None
    if len(v) != 4 or not all(math.isfinite(x) for x in v): return None
    x, y, w, h = v
    return (x, y, w, h) if 0 <= x < 1 and 0 <= y < 1 and 0.02 <= w <= 1 and 0.02 <= h <= 1 and x + w <= 1.001 and y + h <= 1.001 else None

def fmt_region(r): return ",".join(f"{v:.4f}" for v in r)

def region_px(frac, sw, sh):
    """A fractional region as an even-sized crop (x, y, w, h) that stays inside a sw x sh picture."""
    fx, fy, fw, fh = frac
    w, h = min(max(16, int(fw * sw) // 2 * 2), sw // 2 * 2), min(max(16, int(fh * sh) // 2 * 2), sh // 2 * 2)
    return max(0, min(int(round(fx * sw)), sw - w)), max(0, min(int(round(fy * sh)), sh - h)), w, h

def layout_sizes(c):
    """Output width, height and the height of the face cam panel in split mode."""
    rw, rh = map(int, c["aspect"].split(":"))
    W = c["out_width"] // 2 * 2; H = round(W * rh / rw / 2) * 2
    return W, H, min(max(round(H * c["split_cam_pct"] / 100 / 2) * 2, 2), H - 2)

def creator_handle(info):
    """The uploader's @handle from yt-dlp's info, or '@ChannelName' when there is no handle."""
    uid = str(info.get("uploader_id") or "")
    if uid.startswith("@"): return uid
    name = re.sub(r"\s+", "", str(info.get("channel") or info.get("uploader") or ""))
    return "@" + name if name else ""

def build_wm_ass(c, W, H, text):
    """A one-line ASS file that shows the creator credit for the whole clip."""
    k = H / 288.0
    fs = max(8, round(c["wm_size"] * k))
    alpha = "%02X" % round(255 * (1 - max(10, min(100, c["wm_opacity"])) / 100))
    pos = c["wm_position"]
    align = {"top right": 9, "top left": 7, "bottom right": 3, "bottom left": 1}[pos]
    mv, mh = round(H * (0.05 if pos.startswith("top") else 0.07)), round(W * 0.04)
    font = re.sub(r"[,\r\n]", " ", str(c["edit_font"])).strip() or DEFAULT_FONT
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Wm,%s,%d,&H%sFFFFFF,&H000000FF,&H%s000000,&H00000000,-1,0,0,0,100,100,0,0,1,%d,0,%d,%d,%d,%d,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 5,0:00:00.00,9:59:59.00,Wm,,0,0,0,,%s\n"
            % (W, H, font, fs, alpha, alpha, max(1, round(0.8 * k)), align, mh, mh, mv, ass_text(text)))

def track_width(sw, sh, W, H):
    """Width of the window cut from an sw x sh picture so that it has the shape of the W x H output. Equal to sw when no cut is needed."""
    return min(sw, max(2, round(sh * W / H / 2) * 2))

def face_path(samples):
    """Camera path that follows a speaker's face without jitter.
    samples: (time, face centre as a fraction 0-1 of the picture width, or None when no face was seen), in time order.
    Returns (start centre, moves) or None when no face was found at all. Each move is (start time, shift as a fraction of the width, seconds).
    The camera only moves when the face has left a dead zone around it, and one stray detection never moves it."""
    pts = [(t, cx) for t, cx in samples if cx is not None]
    if not pts: return None
    cx = [p[1] for p in pts]
    sm = [sorted(cx[max(0, i - 2):i + 3])[len(cx[max(0, i - 2):i + 3]) // 2] for i in range(len(cx))]  # median of up to 5 neighbours drops stray detections
    cam, free, moves = sm[0], 0.0, []
    for (t, _), c in zip(pts, sm):
        if t < free or abs(c - cam) <= 0.07: continue
        d = c - cam; dur = min(1.2, 0.4 + 1.5 * abs(d))
        moves.append((round(t, 2), round(d, 4), round(dur, 2)))
        cam, free = c, t + dur
    return sm[0], moves

def track_crop(path, sw, sh, cw):
    """ffmpeg crop that cuts a cw x sh window out of the sw x sh picture and slides it along the face path (eased, like the zoom)."""
    x0, moves = path
    terms = "".join("%+.1f*(0.5-0.5*cos(PI*clip((t-%.2f)/%.2f,0,1)))" % (d * sw, t0, dur) for t0, d, dur in moves)
    return "crop=%d:%d:x='clip(%.1f%s,0,%d)':y=0" % (cw, sh, x0 * sw - cw / 2, terms, sw - cw)

def camera_x(path, t):
    """Camera centre (fraction of the picture width) at time t on a face_path()."""
    x0, moves = path
    return x0 + sum(d * (0.5 - 0.5 * math.cos(math.pi * min(1.0, max(0.0, (t - t0) / dur)))) for t0, d, dur in moves)
