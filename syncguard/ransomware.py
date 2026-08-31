"""
SyncGuard ransomware detection — entropy sampling, extension anomaly
detection, and composite anomaly scoring.
"""

import os
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Suspicious extension blocklist
# ---------------------------------------------------------------------------

SUSPICIOUS_EXTENSIONS: Set[str] = {
    # ── WannaCry / WCry family ──────────────────────────────────
    ".wnry", ".wcry", ".wncry", ".wncryt",
    # ── Locky family ────────────────────────────────────────────
    ".locky", ".zepto", ".odin", ".thor", ".aesir", ".diablo6",
    ".ezz", ".ezz", ".cerber", ".cerber3", ".cerber5", ".cerber6",
    ".abc", ".ccc", ".vvv", ".ttt", ".ecc", ".ezz", ".exx",
    ".xyz", ".zzz", ".zzzzz", ".micro", ".xxx",
    # ── Dharma / CrySIS family ───────────────────────────────────
    ".dharma", ".wallet", ".arena", ".bip", ".cobra",
    ".java", ".arrow", ".brrr", ".boost", ".gamma",
    ".monro", ".brrr", ".cezar", ".bleep", ".,onion",
    ".ETH", ".CRAB", ".DEB", ".FROZEN", ".betta",
    ".BRRR", ".AUDIT", ".VIRS", ".LISA", ".phobos",
    ".eking", ".eight", ".ethylamerica", ".makop",
    ".mkp", ".decoder", ".mira", ".flavor", ".EMPRISE",
    # ── CryptoLocker / CryptoWall family ─────────────────────────
    ".crypt", ".crypto", ".encrypted", ".locked",
    ".crypted", ".crypz", ".crypt1", ".crypt2", ".crypt3",
    ".cryptolocker", ".cryptowall",
    # ── GlobeImposter family ─────────────────────────────────────
    ". encrypted", ". abort", ".auchentoshan", ".auodsi",
    ".bad", ".bip", ".cod", ".cobra",
    # ── STOP / Djvu family ───────────────────────────────────────
    ".moia", ".ness", ".omba", ".loce", ".vawai",
    ".boothe", ".lanset", ".kaak", ".moka",
    ".medusa", ".stare", ".lote", ".krogu",
    ".karl", ".wand", ".mol64", ".olgun", ".lkfr",
    ".deria", ".masodas", ".bandar", ".tro",
    ".gero", ".befro", ".liy0", ".nyton", ".ryeco",
    ".liquido", ".allead", ".alcat", ".moba", ".nusm",
    ".kyra", ".exx", ".vega", ".mogera", ".udia",
    ".tro", ".kodg", ".zqqw", ".lecho", ".varies",
    ".makop", ".szig", ".coharos", ".blocked",
    # ── Conti / Ryuk / REvil family ──────────────────────────────
    ".conti", ".ryuk", ".revil", ".sodinokibi",
    ".rkhorse", ".rmar", ".booa", ".elbie",
    ".devos", ".lukits", ".mekos", ".makop",
    # ── Maze / Egregor / NetWalker ───────────────────────────────
    ".maze", ".egregor", ".netwalker", ".cryptomix",
    ".meow", ".meow", ".meow", ".enc",
    # ── Avaddon ──────────────────────────────────────────────────
    ".avdn", ".avdn", ".abensen",
    # ── BlackCat / ALPHV ────────────────────────────────────────
    ".blackcat", ".blackcat", ".alphv",
    # ── LockBit family ───────────────────────────────────────────
    ".lockbit", ".lockbit3.0", ".lockbit2",
    # ── Hive ─────────────────────────────────────────────────────
    ".hive", ".key", ".key.hive",
    # ── Cl0p / Clop ──────────────────────────────────────────────
    ".clop", ".cl0p", ".cl0p",
    # ── Akira ────────────────────────────────────────────────────
    ".akira",
    # ── Phobos ───────────────────────────────────────────────────
    ".phobos", ".eking", ".eight",
    # ── MedusaLocker ─────────────────────────────────────────────
    ".encrypted", ".ReadTheInstructions", ".READINSTRUCTIONS",
    ".READINSTRUCTION", ".READINSTRUCTION.txt",
    # ── Mount Locker / Balanced Lens ─────────────────────────────
    ".lived", ".pazd", ".blocked",
    # ── Maoloa /harma ────────────────────────────────────────────
    ".maoloa", ".harma", ".loa", ".cezar",
    # ── Magniber ─────────────────────────────────────────────────
    ".kinopoisk", ".dodocool",
    # ── STOP variants (additional) ──────────────────────────────
    ".puma", ".luna", ".monro", ".sald",
    # ── Generic / miscellaneous ransomware ───────────────────────
    ".enc", ".ENC", ".EnCiPhErEd", ".ENCRYPTED",
    ".cry", ".crypted", ".ransom", ".ransomed",
    ".vault", ".cryb2", ".ctb2", ".ctbl",
    ".crinf", ".crjoker", ".darkness", ".frtrss",
    ".good", ".ha3", ".hydracrypt", ".kb15",
    ".kraken", ".lechiffre", ".lockedup", ".magic",
    ".nochance", ".omg!", ".LOL!", ".pay", ".paym",
    ".r5a", ".rdm", ".rrk", ".sdjn", ".supercrypt",
    ".toxcrypt", ".bos", ".gdb",
    ".ABYSS", ".avdn", ".clop", ".conti",
    ".dharma", ".hive", ".lockbit", ".makop",
    ".matrix", ".MEOW", ".nightcrow", ".phobos",
    ".ping", ".quantum", ".ryuk", ".snet",
    ".tprc", ".unkno", ".xam",
    ".Lukitus", ".lukits", ".bleep",
    ".xrnt", ".xtbl",
    # ── Numbered / zero-padded variants ──────────────────────────
    ".0x0", ".1999", ".000", ".111", ".222", ".333",
    ".444", ".555", ".666", ".777", ".888", ".999",
}


# ---------------------------------------------------------------------------
# Shannon entropy calculation
# ---------------------------------------------------------------------------

def shannon_entropy(data: bytes) -> float:
    """Calculate Shannon entropy of byte data (0.0 = uniform, 8.0 = random)."""
    if not data:
        return 0.0
    counts = Counter(data)
    length = len(data)
    entropy = 0.0
    for count in counts.values():
        p = count / length
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def file_entropy(filepath: str, sample_size: int = 4096) -> Optional[float]:
    """
    Read up to sample_size bytes from the start of a file and return
    its Shannon entropy. Returns None on read error.
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read(sample_size)
        if not data:
            return 0.0
        return shannon_entropy(data)
    except (OSError, PermissionError):
        return None


# ---------------------------------------------------------------------------
# Entropy sampling
# ---------------------------------------------------------------------------

@dataclass
class EntropyResult:
    """Result of entropy sampling across changed files."""
    sampled:        int   = 0
    high_entropy:   int   = 0
    avg_entropy:    float = 0.0
    max_entropy:    float = 0.0
    flagged_files:  List[str] = field(default_factory=list)
    is_suspicious:  bool  = False


def sample_entropy(
    file_paths: List[str],
    sample_count: int = 20,
    threshold: float = 7.5,
    sample_size: int = 4096,
) -> EntropyResult:
    """
    Sample a random subset of files and check Shannon entropy.
    Encrypted files typically have entropy > 7.5 bits/byte.
    """
    if not file_paths:
        return EntropyResult()

    # Sample up to sample_count files (or all if fewer)
    to_sample = random.sample(
        file_paths, min(sample_count, len(file_paths)))

    entropies = []
    flagged = []

    for fp in to_sample:
        ent = file_entropy(fp, sample_size)
        if ent is not None:
            entropies.append(ent)
            if ent >= threshold:
                flagged.append(fp)

    if not entropies:
        return EntropyResult()

    avg = sum(entropies) / len(entropies)
    mx = max(entropies)

    return EntropyResult(
        sampled=len(entropies),
        high_entropy=len(flagged),
        avg_entropy=round(avg, 3),
        max_entropy=round(mx, 3),
        flagged_files=flagged,
        is_suspicious=len(flagged) >= max(2, len(entropies) // 5),
    )


# ---------------------------------------------------------------------------
# Extension anomaly detection
# ---------------------------------------------------------------------------

@dataclass
class ExtensionResult:
    """Result of extension anomaly analysis."""
    total_changed:   int   = 0
    suspicious:      int   = 0
    suspicious_exts: Dict[str, int] = field(default_factory=dict)
    flagged_files:   List[str] = field(default_factory=list)
    is_suspicious:   bool  = False


def detect_suspicious_extensions(
    changed_files: List[str],
    extra_blocklist: Optional[Set[str]] = None,
    threshold_pct: float = 5.0,
) -> ExtensionResult:
    """
    Check changed files for suspicious new extensions.
    Returns suspicious if >threshold_pct of files have blocked extensions.
    """
    if not changed_files:
        return ExtensionResult()

    blocklist = SUSPICIOUS_EXTENSIONS.copy()
    if extra_blocklist:
        blocklist.update(e.lower() for e in extra_blocklist)

    ext_counts: Dict[str, int] = Counter()
    flagged = []

    for fp in changed_files:
        ext = os.path.splitext(fp)[1].lower()
        if ext in blocklist:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            flagged.append(fp)

    total = len(changed_files)
    pct = (len(flagged) / total * 100) if total > 0 else 0.0

    return ExtensionResult(
        total_changed=total,
        suspicious=len(flagged),
        suspicious_exts=dict(ext_counts),
        flagged_files=flagged,
        is_suspicious=pct >= threshold_pct,
    )


# ---------------------------------------------------------------------------
# Composite anomaly scorer
# ---------------------------------------------------------------------------

@dataclass
class AnomalyScore:
    """Composite anomaly assessment combining multiple signals."""
    change_rate:       float = 0.0
    entropy_flag:      bool  = False
    extension_flag:    bool  = False
    delete_ratio:      float = 0.0
    rename_ratio:      float = 0.0
    score:             float = 0.0
    is_blocked:        bool  = False
    reasons:           List[str] = field(default_factory=list)

    # Sub-results for detailed reporting
    entropy_result:    Optional[EntropyResult] = None
    extension_result:  Optional[ExtensionResult] = None


def compute_anomaly_score(
    change_pct: float,
    total_files: int,
    changed_files: int,
    deleted_files: int = 0,
    renamed_files: int = 0,
    entropy_result: Optional[EntropyResult] = None,
    extension_result: Optional[ExtensionResult] = None,
    block_threshold: float = 60.0,
    # Weighting factors (tunable)
    w_change: float = 0.4,
    w_entropy: float = 25.0,
    w_extension: float = 20.0,
    w_delete: float = 15.0,
    w_rename: float = 10.0,
) -> AnomalyScore:
    """
    Compute a composite anomaly score from multiple signals.

    Score components:
      - change_rate * w_change     (0-40 points for 0-100% change)
      - entropy_flag * w_entropy   (0 or 25 points)
      - extension_flag * w_ext     (0 or 20 points)
      - delete_ratio * w_delete    (0-15 points)
      - rename_ratio * w_rename    (0-10 points)

    Total range: 0-100. Block if > block_threshold.
    """
    reasons = []

    # 1. Change rate component (0-40)
    change_component = change_pct * w_change
    if change_pct > 30:
        reasons.append(
            "High change rate: {:.1f}%".format(change_pct))

    # 2. Entropy component (0 or 25)
    entropy_flag = (entropy_result.is_suspicious
                    if entropy_result else False)
    entropy_component = w_entropy if entropy_flag else 0.0
    if entropy_flag:
        reasons.append(
            "High-entropy (encrypted) files detected: {}/{}".format(
                entropy_result.high_entropy, entropy_result.sampled))

    # 3. Extension component (0 or 20)
    extension_flag = (extension_result.is_suspicious
                      if extension_result else False)
    extension_component = w_extension if extension_flag else 0.0
    if extension_flag:
        exts = ", ".join(
            "{} ({})".format(e, c)
            for e, c in sorted(
                extension_result.suspicious_exts.items(),
                key=lambda x: -x[1]))
        reasons.append(
            "Suspicious extensions: " + exts)

    # 4. Delete ratio component (0-15)
    delete_ratio = (
        deleted_files / total_files * 100
        if total_files > 0 else 0.0)
    delete_component = min(delete_ratio, 100.0) / 100.0 * w_delete
    if delete_ratio > 10:
        reasons.append(
            "Mass deletions: {:.1f}%".format(delete_ratio))

    # 5. Rename ratio component (0-10)
    rename_ratio = (
        renamed_files / total_files * 100
        if total_files > 0 else 0.0)
    rename_component = min(rename_ratio, 100.0) / 100.0 * w_rename
    if rename_ratio > 10:
        reasons.append(
            "Mass renames: {:.1f}%".format(rename_ratio))

    total_score = (
        change_component + entropy_component + extension_component +
        delete_component + rename_component
    )

    return AnomalyScore(
        change_rate=change_pct,
        entropy_flag=entropy_flag,
        extension_flag=extension_flag,
        delete_ratio=round(delete_ratio, 2),
        rename_ratio=round(rename_ratio, 2),
        score=round(total_score, 2),
        is_blocked=total_score > block_threshold,
        reasons=reasons,
        entropy_result=entropy_result,
        extension_result=extension_result,
    )
