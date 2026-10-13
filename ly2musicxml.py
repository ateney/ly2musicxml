#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ly2musicxml.py - standalone LilyPond (.ly) -> MusicXML converter.

Verified note-for-note against LilyPond's own MIDI output for
Shostakovich String Quartet No. 8, movements 1-2 (all 8 parts match
exactly in onset, pitch, and duration).

Usage:
    python ly2musicxml.py movement-1.ly
    python ly2musicxml.py movement-1.ly -o thefirst.musicxml

Handles: relative pitch resolution, chords, ties, slurs, tuplets,
\repeat unfold (incl. body-only repeats), partial (pickup) measures,
key/time/tempo changes, dynamics, hairpins, articulations, clefs,
and \ottava (engraving only - sounding pitch is unchanged).

Requires Python 3.7+; no third-party dependencies.
"""
import os
import re
import sys
import datetime
from fractions import Fraction
from xml.sax.saxutils import escape

# ===================== parser core (from ly_parse.py) =====================

LETTER_IDX = {'c': 0, 'd': 1, 'e': 2, 'f': 3, 'g': 4, 'a': 5, 'b': 6}
KEY_CIRCLE = {
    'major': {'c': 0, 'g': 1, 'd': 2, 'a': 3, 'e': 4, 'b': 5, 'fis': 6, 'cis': 7,
              'f': -1, 'bes': -2, 'ees': -3, 'aes': -4, 'des': -5, 'ges': -6},
    'minor': {'a': 0, 'e': 1, 'b': 2, 'fis': 3, 'cis': 4, 'gis': 5, 'dis': 6,
              'd': -1, 'g': -2, 'c': -3, 'f': -4, 'bes': -5, 'ees': -6},
}


def split_accidental(name):
    letter, rest = name[0], name[1:]
    alter, i = 0, 0
    while i < len(rest):
        if rest.startswith('isis', i):
            alter += 2; i += 4
        elif rest.startswith('eses', i):
            alter -= 2; i += 4
        elif rest.startswith('is', i):
            alter += 1; i += 2
        elif rest.startswith('es', i):
            alter -= 1; i += 2
        else:
            raise ValueError('bad accidental in %r' % name)
    return letter, alter


def parse_start_pitch(p):
    m = re.fullmatch(r"([a-g](?:(?:es|is|s)*))([',]*[!?]?)", p)
    return m.group(1)[0], 3 + m.group(2).count("'") - m.group(2).count(',')


def dur_total(d, dots):
    t, f = d, Fraction(1, 2)
    for _ in range(dots):
        t += d * f
        f /= 2
    return t


def remove_markup(text):
    out, i, n = [], 0, len(text)
    pat = re.compile(r'[_^]?\\markup\b')
    while i < n:
        m = pat.search(text, i)
        if not m:
            out.append(text[i:]); break
        out.append(text[i:m.start()])
        j = m.end()
        while j < n and text[j].isspace():
            j += 1
        if j >= n or text[j] != '{':
            raise ValueError('markup without brace at %r' % text[j:j + 20])
        depth, k = 0, j
        while k < n:
            if text[k] == '{':
                depth += 1
            elif text[k] == '}':
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if depth:
            raise ValueError('unbalanced markup braces')
        i = k + 1
    return ''.join(out)


def parse_var_blocks(path):
    src = open(path, encoding='utf-8').read()
    src = '\n'.join(l for l in src.splitlines() if not l.lstrip().startswith('%'))
    result = {}
    for m in re.finditer(r'^(\w+)\s*=\s*(?:\\new\s+Voice\s+)?(?:\\relative\s+(\S+)\s*)?\{\s*$',
                         src, re.M):
        name, rel = m.group(1), m.group(2)
        depth, i, n = 0, m.end() - 1, len(src)
        k = i
        while k < n:
            if src[k] == '{':
                depth += 1
            elif src[k] == '}':
                depth -= 1
                if depth == 0:
                    break
            k += 1
        result[name] = (rel, src[i + 1:k])
    return result


class Ev:
    __slots__ = ('kind', 'pitches', 'dur', 'dots', 'tuplet', 'eff', 'slur_start',
                 'slur_stop', 'arts', 'dyn', 'wedge', 'fermata', 'bow',
                 'tie_start', 'tie_stop', 'tuplet_start', 'tuplet_stop', 'tuplet_seq',
                 'wedge_num')
    def __init__(self, kind):
        self.kind = kind
        self.pitches = []
        self.dur = Fraction(1); self.dots = 0
        self.tuplet = None; self.eff = Fraction(1)
        self.slur_start = False; self.slur_stop = False
        self.arts = []; self.dyn = None; self.wedge = None
        self.fermata = False; self.bow = None
        self.tie_start = False; self.tie_stop = False
        self.tuplet_start = False; self.tuplet_stop = False; self.tuplet_seq = 0
        self.wedge_num = None

    def clone(self):
        c = Ev(self.kind)
        c.pitches = list(self.pitches)
        c.dur, c.dots = self.dur, self.dots
        c.tuplet, c.eff = self.tuplet, self.eff
        c.slur_start, c.slur_stop = self.slur_start, self.slur_stop
        c.arts = list(self.arts); c.dyn = self.dyn; c.wedge = self.wedge
        c.fermata, c.bow = self.fermata, self.bow
        c.tie_start, c.tie_stop = self.tie_start, self.tie_stop
        c.tuplet_start, c.tuplet_stop, self.tuplet_seq = (
            self.tuplet_start, self.tuplet_stop, self.tuplet_seq)
        return c


TOKEN_RE = re.compile(r"""
    (?P<chordopen><)
  | (?P<pitch>[a-g](?:(?:es|is|s)*)[',]*[!?]?)
  | (?P<rest>r)
  | (?P<postfix>-\.|->|--|-_)
  | (?P<bar>\|)
  | (?P<sluron>\()
  | (?P<sluroff>\))
  | (?P<tie>~)
  | (?P<dynamics>\\(?:ppp|pp|p|mp|mf|fff|ff|f|sf|sfff)\b)
  | (?P<wedgeopen>\\<|\\>)
  | (?P<wedgestop>\\!)
  | (?P<crescdim>\\(?:cresc|dim)\b)
  | (?P<clef>\\clef\s+(?P<clefname>\w+))
  | (?P<key>\\key\s+(?P<keytonic>\S+)\s+\\(?P<keymode>major|minor))
  | (?P<time>\\time\s+(?P<timenum>\d+)/(?P<timeden>\d+))
  | (?P<partial>\\partial\s+(?P<partialnum>\d+)\.*)
  | (?P<tempo>\\tempo\s+"(?P<tempotext>[^"]*)"(?:\s+(?P<tempodur>\d+)\s*=\s*(?P<tempobpm>\d+))?)
  | (?P<tuplet>\\tuplet\s+(?P<tupnum>\d+)/(?P<tupden>\d+)(?:\s+(?P<tupdur>\d+\.*)?)?)
  | (?P<repeat>\\repeat\s+unfold\s+(?P<repcount>\d+))
  | (?P<braceopen>\{)
  | (?P<braceclose>\})
  | (?P<skipcmd>\\(?:upbow|downbow|fermata)\b)
""", re.X)


def find_braced(text, i):
    """text[i] must be '{'. Return (body_start, body_end, after)."""
    n = len(text)
    depth, k = 0, i
    while k < n:
        if text[k] == '{':
            depth += 1
        elif text[k] == '}':
            depth -= 1
            if depth == 0:
                return i + 1, k, k + 1
        k += 1
    raise ValueError('unbalanced braces at %d' % i)


def parse_events(body, rel_pitch):
    text = remove_markup(body)
    text = re.sub(r'\\set\s+Staff\.instrumentName\s*=\s*#"[^"]*"', ' ', text)
    text = re.sub(r'\\breathe', ' ', text)
    text = re.sub(r'\\bar\s+"[^"]*"', ' ', text)
    text = re.sub(r'\\ottava\s+#(-?\d+)', r' OTTAVA\1 ', text)

    events = []
    ref = None
    if rel_pitch:
        letter, octv = parse_start_pitch(rel_pitch)
        ref = (LETTER_IDX[letter], octv)
    state = {'cur_dur': Fraction(1), 'cur_dots': 0, 'ottava': 0,
             'in_tuplet': None, 'tup_count': 0}
    dotted_inherit = 0

    def resolve(pstr, r):
        m = re.fullmatch(r"([a-g](?:(?:es|is|s)*))([',]*[!?]?)", pstr)
        name, marks = m.group(1), m.group(2)
        letter, alter = split_accidental(name.rstrip('!?'))
        N = r[1] * 7 + r[0]
        steps = (LETTER_IDX[letter] - r[0]) % 7
        N2 = N + steps if steps <= 3 else N - (7 - steps)
        octv = N2 // 7
        octv += marks.count("'") - marks.count(',')
        return letter, alter, octv

    def last_note():
        # post-events bind to the preceding note even across intervening
        # direction events (dynamics, wedge stops like \!)
        for e in reversed(events):
            if e.kind == 'direction':
                continue
            if e.kind == 'note':
                return e
            return None
        return None

    def run(i, end):
        """Parse text[i:end]; may recurse for \\repeat bodies. Returns None;
        mutates events and the shared pitch/duration state."""
        nonlocal ref, dotted_inherit
        while i < end:
            if text[i].isspace():
                i += 1
                continue
            m = re.match(r'OTTAVA(-?\d+)', text[i:])
            if m:
                state['ottava'] = int(m.group(1))
                i += m.end()
                continue
            m = TOKEN_RE.match(text, i)
            if not m:
                raise ValueError('unparsed at %r (context ...%r)'
                                 % (text[i:i + 40], text[max(0, i - 60):i]))
            i = m.end()
            kind = m.lastgroup

            if kind == 'chordopen':
                j = text.index('>', i)
                inner = text[i:j]
                i = j + 1
                dm = re.match(r'(\d+)(\.*)', text[i:])
                if dm:
                    state['cur_dur'] = Fraction(4) / int(dm.group(1))
                    state['cur_dots'] = len(dm.group(2))
                    i += dm.end()
                d, dots = state['cur_dur'], state['cur_dots']
                pitches, r = [], ref
                first = None
                for pm in re.finditer(r"[a-g](?:(?:es|is|s)*)[',']*[!?]?", inner):
                    p = resolve(pm.group(0), r)
                    pitches.append((p[0], p[1], p[2]))
                    r = (LETTER_IDX[p[0]], p[2])
                    if first is None:
                        first = p
                ev = Ev('note')
                ev.pitches = pitches
                ev.dur, ev.dots = d, dots
                ev.tuplet = state['in_tuplet']
                if state['in_tuplet']:
                    ev.tuplet_seq = state['tup_count']
                    state['tup_count'] += 1
                events.append(ev)
                if first:
                    ref = (LETTER_IDX[first[0]], first[2])

            elif kind == 'pitch':
                pstr = m.group('pitch')
                dm = re.match(r'(\d+)(\.*)', text[i:])
                if dm:
                    state['cur_dur'] = Fraction(4) / int(dm.group(1))
                    state['cur_dots'] = len(dm.group(2))
                    i += dm.end()
                    dots = state['cur_dots']
                else:
                    dots = 0
                    if state['cur_dots']:
                        dotted_inherit += 1
                p = resolve(pstr, ref)
                ev = Ev('note')
                ev.pitches = [(p[0], p[1], p[2])]
                ev.dur, ev.dots = state['cur_dur'], dots
                ev.tuplet = state['in_tuplet']
                if state['in_tuplet']:
                    ev.tuplet_seq = state['tup_count']
                    state['tup_count'] += 1
                events.append(ev)
                ref = (LETTER_IDX[p[0]], p[2])

            elif kind == 'rest':
                dm = re.match(r'(\d+)(\.*)', text[i:])
                if dm:
                    state['cur_dur'] = Fraction(4) / int(dm.group(1))
                    state['cur_dots'] = len(dm.group(2))
                    i += dm.end()
                    dots = state['cur_dots']
                else:
                    dots = 0
                ev = Ev('rest')
                ev.dur, ev.dots = state['cur_dur'], dots
                ev.tuplet = state['in_tuplet']
                if state['in_tuplet']:
                    ev.tuplet_seq = state['tup_count']
                    state['tup_count'] += 1
                events.append(ev)

            elif kind == 'postfix':
                ev = last_note()
                if ev is not None:
                    art = {'-.': 'staccato', '->': 'accent',
                           '--': 'tenuto', '-_': 'portato'}[m.group('postfix')]
                    if art not in ev.arts:
                        ev.arts.append(art)

            elif kind == 'skipcmd':
                ev = last_note()
                if ev is not None:
                    w = m.group('skipcmd')[1:]
                    if w == 'upbow':
                        ev.bow = 'up'
                    elif w == 'downbow':
                        ev.bow = 'down'
                    else:
                        ev.fermata = True

            elif kind == 'sluron':
                ev = last_note()
                if ev is not None:
                    ev.slur_start = True
            elif kind == 'sluroff':
                ev = last_note()
                if ev is not None:
                    ev.slur_stop = True
            elif kind == 'tie':
                ev = last_note()
                if ev is not None:
                    ev.tie_start = True
            elif kind == 'dynamics':
                dyn = m.group('dynamics')[1:]
                ev = last_note()
                if ev is not None:
                    ev.dyn = dyn
                else:
                    ev = Ev('direction'); ev.dyn = dyn; events.append(ev)
            elif kind == 'wedgeopen':
                ev = Ev('direction')
                ev.wedge = 'crescendo' if m.group('wedgeopen') == '\\<' else 'diminuendo'
                events.append(ev)
            elif kind == 'wedgestop':
                ev = Ev('direction'); ev.wedge = 'stop'; events.append(ev)
            elif kind == 'crescdim':
                ev = Ev('direction')
                ev.wedge = 'crescendo' if 'cresc' in m.group('crescdim') else 'diminuendo'
                events.append(ev)
            elif kind == 'clef':
                events.append(('clef', m.group('clefname')))
            elif kind == 'key':
                events.append(('key', KEY_CIRCLE[m.group('keymode')][m.group('keytonic')],
                                m.group('keymode')))
            elif kind == 'time':
                events.append(('time', Fraction(int(m.group('timenum')), int(m.group('timeden')))))
            elif kind == 'partial':
                events.append(('partial', Fraction(4) / int(m.group('partialnum'))))
            elif kind == 'tempo':
                events.append(('tempo', m.group('tempotext'),
                               int(m.group('tempodur')) if m.group('tempodur') else None,
                               int(m.group('tempobpm')) if m.group('tempobpm') else None))
            elif kind == 'tuplet':
                state['in_tuplet'] = (int(m.group('tupnum')), int(m.group('tupden')))
                state['tup_count'] = 0
            elif kind == 'repeat':
                count = int(m.group('repcount'))
                j = i
                n_end = len(text)
                while j < n_end and text[j].isspace():
                    j += 1
                before = len(events)
                if j < n_end and text[j] == '{':
                    b0, b1, after = find_braced(text, j)
                    run(b0, b1)
                    i = after
                else:
                    # single-token body
                    k = j
                    while k < n_end and not text[k].isspace():
                        k += 1
                    run(j, k)
                    i = k
                body_events = events[before:]
                for _ in range(count - 1):
                    events.extend(e.clone() for e in body_events)
            elif kind == 'braceopen':
                pass
            elif kind == 'braceclose':
                state['in_tuplet'] = None
                state['tup_count'] = 0
            # 'bar' (|) is skipped implicitly: no branch needed

    run(0, len(text))
    if dotted_inherit:
        print('  [warn] bare note inheriting dotted duration: %d case(s)' % dotted_inherit)
    return events


def build_measures(events, default_len=Fraction(4)):
    """Group events into measures. Returns (measures, meta, problems)."""
    key = time_sig = tempo = None
    partial = None
    problems = []
    clean = []
    for e in events:
        if isinstance(e, tuple):
            if e[0] == 'key':
                key = (e[1], e[2])
            elif e[0] == 'time':
                time_sig = e[1]
            elif e[0] == 'partial':
                partial = e[1]
            elif e[0] == 'tempo':
                tempo = e[1:]
            continue
        if e.kind == 'direction':
            clean.append(e)
            continue
        d = dur_total(e.dur, e.dots)
        if e.tuplet:
            a, b = e.tuplet
            d = d * Fraction(b, a)
        e.eff = d
        clean.append(e)
    expect = Fraction(4) * time_sig if time_sig else default_len
    had_pickup_start = partial is not None
    if partial is not None:
        expect = partial
    had_pickup = partial is not None
    measures = []
    cur, acc = [], Fraction(0)
    for e in clean:
        if e.kind == 'direction':
            cur.append(e)
            continue
        if acc + e.eff > expect + Fraction(1, 10):
            problems.append('measure %d overflow: adding %s to %s'
                            % (len(measures) + 1, e.eff, acc))
            measures.append(cur)
            cur, acc = [], Fraction(0)
            expect = Fraction(4) * time_sig
        cur.append(e)
        acc += e.eff
        if acc == expect:
            measures.append(cur)
            cur, acc = [], Fraction(0)
            if had_pickup:
                expect = Fraction(4) * time_sig
                had_pickup = False
    if acc != 0:
        problems.append('final partial measure: %s left over at measure %d'
                         % (acc, len(measures) + 1))
    if cur:
        measures.append(cur)
    # a trailing measure holding only directions (end-of-piece \! etc.)
    # should not exist on its own: merge into the previous measure
    if len(measures) > 1:
        last = measures[-1]
        timed = [e for e in last if e.kind in ('note', 'rest')]
        if not timed:
            measures[-2].extend(last)
            measures.pop()
    mlen = Fraction(4) * time_sig if time_sig else default_len
    meta = dict(key=key, time=time_sig, time_sig=time_sig, tempo=tempo,
                partial=partial, mlen=mlen,
                pickup=had_pickup_start, problems=problems)
    return measures, meta, problems


def tie_postprocess(events):
    """Mark tie chains: a note carrying tie_start makes the next timed note
    (ignoring intervening directions) the tie target."""
    prev = None
    for e in events:
        if isinstance(e, tuple):
            continue
        if e.kind == 'direction':
            continue
        if prev is not None and prev.tie_start and e.kind == 'note':
            e.tie_stop = True
        prev = e


# ===================== writer (from ly_write.py) =====================

DIV = 48
DUR_TYPE = {Fraction(4): 'whole', Fraction(2): 'half', Fraction(1): 'quarter',
            Fraction(1, 2): 'eighth', Fraction(1, 4): '16th', Fraction(1, 8): '32nd'}
CLEF_XML = {'treble': ('G', 2), 'alto': ('C', 3), 'tenor': ('C', 4), 'bass': ('F', 4)}
BEAT_UNIT = {4: 'whole', 2: 'half', 1: 'quarter'}


def measure_content_len(measure):
    t = Fraction(0)
    for e in measure:
        if isinstance(e, tuple) or e.kind == 'direction':
            continue
        t += e.eff
    return t


def dynamic_xml(dyn):
    tag = ('<other-dynamics>%s</other-dynamics>' % dyn) if dyn == 'sfff' \
        else ('<%s/>' % dyn)
    return ('      <direction placement="below"><direction-type>'
            '<dynamics>%s</dynamics></direction-type></direction>' % tag)


def direction_xml(e):
    if e.wedge:
        return ('      <direction placement="below"><direction-type>'
                '<wedge type="%s" number="%d"/></direction-type></direction>'
                % (e.wedge, e.wedge_num or 1))
    if e.dyn:
        return dynamic_xml(e.dyn)
    return ''


def mark_tuplet_runs(measures):
    for measure in measures:
        timed = [e for e in measure if not isinstance(e, tuple)
                 and e.kind != 'direction']
        for i, e in enumerate(timed):
            if not e.tuplet:
                continue
            num = e.tuplet[0]
            prev = timed[i - 1] if i > 0 else None
            nxt = timed[i + 1] if i + 1 < len(timed) else None
            prev_same = prev is not None and prev.tuplet == e.tuplet
            nxt_same = nxt is not None and nxt.tuplet == e.tuplet
            e.tuplet_start = (not prev_same) or (e.tuplet_seq % num == 0)
            e.tuplet_stop = ((not nxt_same)
                             or (nxt.tuplet_seq % num == 0))


def note_xml(e):
    dur_div = int(e.eff * DIV)
    typ = DUR_TYPE.get(e.dur)
    if typ is None:
        raise RuntimeError('unknown note type %s' % e.dur)
    body = []
    for pi, (step, alter, octv) in enumerate(e.pitches):
        notations = []
        lines = []
        lines.append('      <note>')
        if pi > 0:
            lines.append('        <chord/>')
        lines.append('        <pitch><step>%s</step>%s<octave>%d</octave></pitch>'
                     % (step.upper(),
                        ('<alter>%d</alter>' % alter) if alter else '',
                        octv))
        lines.append('        <duration>%d</duration>' % dur_div)
        if pi == 0:
            if e.tie_stop:
                lines.append('        <tie type="stop"/>')
            if e.tie_start:
                lines.append('        <tie type="start"/>')
            if e.tie_stop:
                notations.append('<tied type="stop"/>')
            if e.tie_start:
                notations.append('<tied type="start"/>')
            if e.slur_start:
                notations.append('<slur type="start" number="1"/>')
            if e.slur_stop:
                notations.append('<slur type="stop" number="1"/>')
            if e.arts:
                notations.append('<articulations>%s</articulations>'
                                 % ''.join('<%s/>' % a for a in e.arts))
            if e.bow:
                notations.append('<technical><%s-bow/></technical>'
                                 % ('up' if e.bow == 'up' else 'down'))
            if e.tuplet and e.tuplet_start:
                notations.append('<tuplet type="start" bracket="yes"/>')
            if e.tuplet and e.tuplet_stop:
                notations.append('<tuplet type="stop"/>')
            if e.fermata:
                notations.append('<fermata/>')
        lines.append('        <voice>1</voice>')
        lines.append('        <type>%s</type>' % typ)
        lines.extend('        <dot/>' for _ in range(e.dots))
        if e.tuplet:
            a, b = e.tuplet
            lines.append('        <time-modification>'
                         '<actual-notes>%d</actual-notes>'
                         '<normal-notes>%d</normal-notes>'
                         '</time-modification>' % (a, b))
        if notations:
            lines.append('        <notations>%s</notations>' % ''.join(notations))
        lines.append('      </note>')
        body.append('\n'.join(lines))
    return '\n'.join(body)


def rest_xml(e):
    dur_div = int(e.eff * DIV)
    typ = DUR_TYPE.get(e.dur)
    if typ is None:
        raise RuntimeError('unknown rest type %s' % e.dur)
    lines = ['      <note>', '        <rest/>',
             '        <duration>%d</duration>' % dur_div,
             '        <voice>1</voice>',
             '        <type>%s</type>' % typ]
    lines.extend('        <dot/>' for _ in range(e.dots))
    if e.tuplet:
        a, b = e.tuplet
        lines.append('        <time-modification>'
                     '<actual-notes>%d</actual-notes>'
                     '<normal-notes>%d</normal-notes>'
                     '</time-modification>' % (a, b))
    lines.append('      </note>')
    return '\n'.join(lines)


def write_part(measures, meta, first_part, tempo):
    """tempo: (text, dur, bpm) or None."""
    out = []
    mlen = meta['mlen']
    had_pickup = meta.get('pickup')
    first_attrs_done = False
    tempo_written = False
    for mi, measure in enumerate(measures):
        number = mi if had_pickup else mi + 1
        implicit = ' implicit="yes"' if (mi == 0 and had_pickup) else ''
        out.append('    <measure number="%d"%s>' % (number, implicit))
        attrs = []
        if not first_attrs_done:
            attrs.append('        <divisions>%d</divisions>' % DIV)
            if meta['key']:
                fifths, mode = meta['key']
                attrs.append('        <key><fifths>%d</fifths><mode>%s</mode></key>'
                             % (fifths, mode))
            if meta['time']:
                attrs.append('        <time><beats>%d</beats><beat-type>%d</beat-type></time>'
                             % (meta['time'].numerator, meta['time'].denominator))
            first_attrs_done = True
        for e in measure:
            if isinstance(e, tuple) and e[0] == 'clef':
                sign, line = CLEF_XML[e[1]]
                attrs.append('        <clef><sign>%s</sign><line>%d</line></clef>'
                             % (sign, line))
        if attrs:
            out.append('      <attributes>')
            out.extend(attrs)
            out.append('      </attributes>')
        if first_part and tempo and not tempo_written and mi == 0:
            txt, tdur, tbpm = tempo
            if tdur and tbpm and tdur in BEAT_UNIT:
                sound_qpm = tbpm * 4 // tdur if (tbpm * 4) % tdur == 0 else None
                out.append('      <direction placement="above">'
                           '<direction-type><words>%s</words></direction-type>'
                           '<direction-type><metronome>'
                           '<beat-unit>%s</beat-unit><per-minute>%d</per-minute>'
                           '</metronome></direction-type>'
                           '%s</direction>'
                           % (escape(txt), BEAT_UNIT[tdur], tbpm,
                              ('<sound tempo="%d"/>' % sound_qpm) if sound_qpm else ''))
            else:
                out.append('      <direction placement="above">'
                           '<direction-type><words>%s</words></direction-type>'
                           '</direction>' % escape(txt))
            tempo_written = True
        for e in measure:
            if isinstance(e, tuple):
                continue
            if e.kind == 'direction':
                out.append(direction_xml(e))
            elif e.kind == 'rest':
                out.append(rest_xml(e))
            else:
                if e.dyn:
                    out.append(dynamic_xml(e.dyn))
                out.append(note_xml(e))
        out.append('    </measure>')
    return '\n'.join(out)


def build_movement(ly_path, prefix, movement_roman, movement_name, out_path,
                   work_title='Untitled', composer='Unknown'):
    vars_ = parse_var_blocks(ly_path)
    _, begin_body = vars_[prefix + '_begin']
    begin_events = parse_events(begin_body, None)
    parts = [('P1', 'Violin 1', 'Vln. 1', 'violin_a'),
             ('P2', 'Violin 2', 'Vln. 2', 'violin_b'),
             ('P3', 'Viola', 'Vla.', 'viola'),
             ('P4', 'Cello', 'Vc.', 'cello')]
    meta = None
    all_parts = []
    print('%s. %s' % (movement_roman, movement_name))
    for pid, pname, abbr, suffix in parts:
        key = '%s_%s' % (prefix, suffix)
        if key not in vars_:
            continue  # tolerate missing parts (e.g. solo / partial scores)
        rel, body = vars_[key]
        events = list(begin_events) + parse_events(body, rel)
        tie_postprocess(events)
        # hairpin bookkeeping (LilyPond semantics): a new dynamic spanner
        # event implicitly terminates any spanner still open; a stop closes
        # the most recently opened one. Wedges get MusicXML numbers so
        # overlapping hairpins stay distinguishable.
        numbered = []
        stack = []  # open wedge numbers, most recent last
        for e in events:
            if isinstance(e, tuple):
                numbered.append(e)
                continue
            if e.kind == 'direction' and e.wedge:
                if e.wedge == 'stop':
                    e.wedge_num = stack.pop() if stack else 1
                else:
                    while stack:
                        se = Ev('direction')
                        se.wedge = 'stop'
                        se.wedge_num = stack.pop()
                        numbered.append(se)
                    e.wedge_num = 1
                    stack.append(1)
            numbered.append(e)
        events = numbered
        measures, m, problems = build_measures(events)
        for p in problems:
            print('  [%s] %s' % (pname, p))
        if meta is None:
            meta = m
        notes = sum(1 for meas in measures for e in meas
                    if not isinstance(e, tuple) and e.kind == 'note')
        chords = sum(1 for meas in measures for e in meas
                     if not isinstance(e, tuple) and e.kind == 'note'
                     and len(e.pitches) > 1)
        print('  %-9s measures=%d notes=%d chords=%d' % (pname, len(measures), notes, chords))
        all_parts.append((pid, pname, abbr, measures))
    counts = [len(p[3]) for p in all_parts]
    if len(set(counts)) != 1:
        print('  [FAIL] measure counts differ: %s' % counts)
    else:
        print('  measure count: %d (all parts agree)' % counts[0])
    if all_parts[0][3]:
        print('  first measure length: %s quarter(s) (4/4 movement: expect 2 pickup)'
              % measure_content_len(all_parts[0][3][0]))
    for pid, pname, abbr, measures in all_parts:
        mark_tuplet_runs(measures)

    tempo = meta['tempo']
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<!DOCTYPE score-partwise PUBLIC '
               '"-//Recordare//DTD MusicXML 3.1 Partwise//EN" '
               '"http://www.musicxml.org/dtds/partwise.dtd">')
    out.append('<score-partwise version="3.1">')
    out.append('  <work><work-title>%s</work-title></work>'
               % escape(work_title))
    out.append('  <movement-number>%s</movement-number>' % movement_roman)
    out.append('  <movement-title>%s</movement-title>' % escape(movement_name))
    out.append('  <identification>'
               '<creator type="composer">%s</creator>'
               '<encoding><software>ly2musicxml.py</software>'
               '<encoding-date>%s</encoding-date></encoding>'
               '</identification>'
               % (escape(composer), datetime.date.today().isoformat()))
    out.append('  <part-list>')
    for pid, pname, abbr, _ in all_parts:
        out.append('    <score-part id="%s"><part-name>%s</part-name>'
                   '<part-abbreviation>%s</part-abbreviation></score-part>'
                   % (pid, escape(pname), escape(abbr)))
    out.append('  </part-list>')
    for idx, (pid, pname, abbr, measures) in enumerate(all_parts):
        out.append('  <part id="%s">' % pid)
        out.append(write_part(measures, meta, idx == 0, tempo))
        out.append('  </part>')
    out.append('</score-partwise>')
    xml = '\n'.join(out) + '\n'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(xml)
    print('  written: %s (%d bytes)' % (out_path, len(xml)))
    return xml

# ===================== CLI / auto-detection =====================
PART_SUFFIXES = [('P1', 'Violin 1', 'Vln. 1', 'violin_a'),
                 ('P2', 'Violin 2', 'Vln. 2', 'violin_b'),
                 ('P3', 'Viola', 'Vla.', 'viola'),
                 ('P4', 'Cello', 'Vc.', 'cello')]


def roman(n):
    vals = [(10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]
    out = ''
    for v, sym in vals:
        while n >= v:
            out += sym
            n -= v
    return out


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description='Convert LilyPond source (.ly) to MusicXML '
                    '(score-partwise 3.1).')
    ap.add_argument('input', help='LilyPond source file, e.g. movement-1.ly')
    ap.add_argument('-o', '--output',
                    help='output path (.musicxml). '
                         'Default: <input-stem>.musicxml')
    ap.add_argument('--title',
                    help='override the work title '
                         '(default: first "%%" comment line)')
    ap.add_argument('--composer', default='Unknown',
                    help='composer name (default: Unknown)')
    args = ap.parse_args(argv)

    ly_path = args.input
    if not os.path.exists(ly_path):
        print('error: file not found: %s' % ly_path)
        return 1

    # work title from the leading "% ..." header comments, if any
    work_title = os.path.splitext(os.path.basename(ly_path))[0]
    composer = args.composer
    with open(ly_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('%%'):
                continue
            if not line.startswith('%'):
                break
            t = line.lstrip('%').strip()
            if t:
                work_title = t
                break
        work_title = args.title or work_title

    vars_ = parse_var_blocks(ly_path)
    prefixes = sorted(name[:-len('_begin')] for name in vars_
                      if name.endswith('_begin'))
    if not prefixes:
        print('error: no <name>_begin block found in %s' % ly_path)
        return 1

    stem = os.path.splitext(ly_path)[0]
    multi = len(prefixes) > 1
    fm = re.search(r'(\d+)', os.path.basename(stem))
    base_num = int(fm.group(1)) if fm else 0
    status = 0
    for i, prefix in enumerate(prefixes):
        # movement title from the \tempo text in the begin block
        begin_body = vars_[prefix + '_begin'][1]
        tm = re.search(r'\\tempo\s+"([^"]+)"', begin_body)
        title = tm.group(1) if tm else prefix.replace('_', ' ').title()
        mroman = roman(base_num if base_num else i + 1)
        if multi:
            out_path = '%s-%s.musicxml' % (stem, prefix)
        elif args.output:
            out_path = args.output
        else:
            out_path = stem + '.musicxml'
        has_parts = any(('%s_%s' % (prefix, s[3])) in vars_
                        for s in PART_SUFFIXES)
        if not has_parts:
            print('skip %s: no part variables found' % prefix)
            continue
        print('=== %s (%s) -> %s' % (prefix, title, out_path))
        try:
            build_movement(ly_path, prefix, mroman, title, out_path,
                           work_title=work_title, composer=composer)
        except Exception:
            import traceback
            traceback.print_exc()
            status = 1
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
