#!/usr/bin/env python3

import sys, os, tempfile
import re, base64
import socket, ssl
from typing import Union
from io import BytesIO
#from weasyprint import HTML, CSS       # moved below to improve performance
                                        # if weasyprint is not used.
from urllib.parse import urlparse, urljoin, quote as urlquote,\
                                            unquote as urlunquote
from html import escape as html_escape
from mimetypes import guess_extension
from getopt import gnu_getopt as getopt
from copy import deepcopy


magic_line = '%♊\ufe0e🗎\ufe0e'


class GemdocClientException(Exception):
    pass

def retrieve_url(url: str, max_redirects=10) -> tuple[str,str,Union[str,bytes]]:
    """
    Returns a tuple of type (url, content), where url is possibly
    different from the one supplied as an argument if there have been
    any redirects.
    """
    if max_redirects <= 0:
        raise GemdocClientException('Maximum number of redirects exceeded')
    scheme, host, *_ = urlparse(url); port = 1965
    content = list()
    if scheme != 'gemini':
        raise GemdocClientException(f'Unsupported url scheme {scheme}')
    if ':' in host: host, port = host.rsplit(':', maxsplit=1)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port))) as sock:
        with context.wrap_socket(sock, server_hostname=host) as ssock:
            ssock.send(f'{url}\r\n'.encode('utf-8'))
            response = ssock.recv(1029)
            # I am not entirely sure why the loop below is needed, but in
            # some cases I only get the status code on the first recv, so
            # I need to call recv multiple times to fetch the whole status
            # line.
            while len(response) < 1029:
                response += ssock.recv(1029-len(response))
            if b'\r\n' not in response:
                raise GemdocClientException('Server response too long')
            header, rest = response.split(b'\r\n', maxsplit=1)
            header = header.decode('utf-8')
            if not header[:2].isnumeric():
                raise GemdocClientException('Invalid response from server')
            if header.startswith('3'):
                dest = header[3:]
                print(f"Following redirect to '{dest}'", file=sys.stderr)
                return retrieve_url(dest, max_redirects=max_redirects-1)
            elif header.startswith('2'):
                mime_type, *params = header[3:].split(';')
                mime_type = mime_type.strip().lower()
                charset = 'utf-8'
                for p in params:
                    k, *v = p.strip().split('=', maxsplit=1)
                    k, v = k.strip().lower(), v[0].strip() if v else ''
                    if k == 'charset': charset = v
                doc = [rest]
                while True:
                    next = ssock.recv(1024)
                    if not next: break
                    doc.append(next)
                doc = b''.join(doc)
                if mime_type.startswith('text/'): doc = doc.decode(charset)
                return url, mime_type, doc
            else:
                raise GemdocClientException(f"Server replied: '{header}'")


class GemdocPDFObject():
    def _consume_whitespace(self, binary: bytes) -> bytes:
        m = re.search(rb'[^\s]', binary)
        if m: return binary[m.start():]
        return binary
    def _consume_objnum(self, binary: bytes) -> tuple[bytes,bytes]:
        binary = self._consume_whitespace(binary)
        m = re.match(rb'^(\d+)\s+(\d+)\s+obj[\s]+', binary)
        if not m:
            raise Exception('No object at the start of '+str(binary[:10]))
        objnum = '{} {} obj'.format(*[x.decode('ascii') for x in m.groups()])
        return binary[m.end():], objnum.encode('ascii')
    def _consume_list(self, binary: bytes, delim=(b'[',b']')) -> tuple[bytes,dict]:
        binary = self._consume_whitespace(binary)
        if not binary.startswith(delim[0]):
            raise Exception('Expected '+str(delim[0])+' at the start of '\
                            +str(binary[:10]))
        binary = binary[len(delim[0]):]
        d = list()
        while True:
            binary = self._consume_whitespace(binary)
            if binary.startswith(b'%'):
                # Strip all comments from within dictionaries
                m = re.search(rb'[\r\n]', binary)
                binary = b'' if not m else binary[m.start()+1:]
            elif binary.startswith(b'/'):
                end = re.search(rb'[\s\(\)<>\[\]{}/%]', binary[1:]).start()+1
                key, binary = binary[:end], binary[end:]
                d.append(key)
            elif binary.startswith(b'('):
                o, c = binary.find(b'(', 1), binary.find(b')', 1)
                while 0 <= o < c:
                    o, c = binary.find(b'(', c+1), binary.find(b')', c+1)
                end = c+1
                key, binary = binary[:end], binary[end:]
                d.append(key)
            elif binary.startswith(b'['):
                binary, l = self._consume_list(binary)
                d.append(l)
            elif binary.startswith(b'<<'):
                binary, key = self._consume_dictionary(binary)
                d.append(key)
            elif binary.startswith(b'<'):
                end = binary.index(b'>')+1
                key, binary = binary[:end], binary[end:]
                d.append(key)
            elif re.match(b'^\d+\s+\d+\s+R', binary):
                end = binary.index(b'R')+1
                key, binary = binary[:end], binary[end:]
                d.append(key)
            elif re.match(b'^[\d-]', binary):
                end = re.search(rb'[^\d\.-]', binary).start()
                key, binary = binary[:end], binary[end:]
                d.append(key)
            elif binary.startswith(b'null'):
                d.append(b'null'); binary = binary[4:]
            elif binary.startswith(b'true'):
                d.append(b'true'); binary = binary[4:]
            elif binary.startswith(b'false'):
                d.append(b'false'); binary = binary[5:]
            elif binary.startswith(delim[1]):
                binary = binary[len(delim[1]):]
                break
            else:
                raise Exception('Unknown list item at '+str(binary[:10]))
        return binary, d
    def _consume_dictionary(self, binary: bytes) -> tuple[bytes,dict]:
        binary, l = self._consume_list(binary, delim=(b'<<',b'>>'))
        d = dict()
        while l:
            if len(l) < 2:
                raise Exception(f'Non-matched last object in dictionary: {l}')
            k, v = l.pop(0), l.pop(0); d[k] = v
        return binary, d
    def _serialize_list(self, l: list, delim=(b'[',b']')) -> bytes:
        for i in range(len(l)):
            if type(l[i]) == dict:
                l[i] = self._serialize_dictionary(l[i])
            elif type(l[i]) == list:
                l[i] = self._serialize_list(l[i])
            elif re.match(b'^[\d-]', l[i]) \
                 or l[i] in [b'null', b'true', b'false']:
                l[i] = b' '+l[i]
        result = b''.join(l)
        if delim[0] in [b'[', b'<<']: result = result.lstrip(b' ')
        if delim[1] in [b']', b'>>']: result = result.rstrip(b' ')
        return delim[0]+result+delim[1]
    def _serialize_dictionary(self, dictionary: dict) -> bytes:
        items = list()
        for k, v in dictionary.items():
            v = self._serialize_list([v], delim=(b'',b''))
            items.extend([k, v])
        return b'<<'+b''.join(items)+b'>>'
    def __init__(self, binary: bytes):
        binary, self._objnum = self._consume_objnum(binary)
        if binary.startswith(b'<<'):
            binary, self.dictionary = self._consume_dictionary(binary)
        else:
            self.dictionary = dict()
        binary = self._consume_whitespace(binary)
        if binary.startswith(b'stream\n'):
            endstream = binary.find(b'endstream')
            if endstream == -1: raise Exception('Missing endstream keyword')
            self._stream = binary[len(b'stream\n'):endstream]
            self._contents = None
        else:
            endobj = binary.find(b'endobj')
            if endobj == -1: raise Exception('Missing endobj keyword')
            self._contents = binary[:endobj]
            self._stream = None
    def serialize(self) -> bytes:
        dictionary = deepcopy(self.dictionary)
        flist = dictionary.pop(b'/Filter', [])
        if type(flist) == bytes: flist = [flist]
        if self._stream != None:
            stream = base64.a85encode(self._stream)+b'~>'
            binary = (b'\rstream\n' + stream + b'\rendstream\r')
            flist.insert(0, b'/ASCII85Decode')
        else:
            binary = self._contents.replace(b'\n', b'\r')
        if flist:
            dictionary[b'/Filter'] = flist[0] if len(flist) == 1 else flist
        if b'/Length' in dictionary:
            dictionary[b'/Length'] = str(len(stream)).encode('ascii')
        if b'/Length1' in dictionary:
            _ = dictionary.pop(b'/Length1')
        binary = self._objnum + b'\r' + \
                 (self._serialize_dictionary(dictionary)
                                            if dictionary else b'') + \
                 binary
        if not binary.endswith(b'\r'): binary += b'\r'
        binary += b'endobj\r'
        return binary
class GemdocPDFTrailer(GemdocPDFObject):
    def __init__(self, binary: bytes):
        super().__init__(b'0 0 obj\n'+binary+b'\nendobj')
    def serialize(self) -> bytes:
        return b'trailer\r'+self._serialize_dictionary(self.dictionary)+b'\r'

class GemdocPDF():
    def _discard_pre_obj(self, binary: bytes) -> tuple[bytes,bytes]:
        m = re.search(rb'\d+\s+\d+\s+o', binary)
        if not m: return b''
        return binary[m.start():]
    def _consume_obj(self, binary: bytes) -> tuple[int,bytes,bytes]:
        m = re.match(rb'\s*(\d+)\s+(\d+)\s+obj', binary)
        main, sub = m.groups()
        if sub != b'0':
            raise Exception('Object revisions not implemented. Unable to parse '\
                            +str(binary[:20]))
        objnum = int(main.decode('ascii'))
        endobj = binary.find(b'endobj')+len(b'endobj')
        if endobj == -1: raise Exception('Missing endobj keyword')
        return objnum, binary[:endobj]+b'\n', binary[endobj:]
    def __init__(self, gemini: str, binary: Union[bytes,str],
                 gemini_filename='source.gmi'):
        if type(binary) == str: binary = binary.encode('utf-8')
        self._gemini = gemini
        self._objects = dict()
        self._trailer = GemdocPDFTrailer(b'')
        while binary:
            if re.match(rb'[\r\n]xref', binary):
                s, e = binary.find(b'trailer'), binary.find(b'startxref')
                if 0 <= s < e-1:
                    s += len(b'trailer')
                    self._trailer = GemdocPDFTrailer(binary[s:e-1])
                eof = binary.find(b'%%EOF')
                binary = binary[eof+len(b'%%EOF'):] if eof > -1 else b''
            else:
                binary = self._discard_pre_obj(binary)
                if not binary: break
                objnum, obj, binary = self._consume_obj(binary)
                self._objects[objnum] = GemdocPDFObject(obj)
        if gemini != None:
            self._gemini_objnum = max(self._objects.keys())+1
            self._make_attachment(self._gemini_objnum, gemini_filename)
    def _make_attachment(self, gemini_objnum, gemini_filename):
        root_ref = self._trailer.dictionary.get(b'/Root')
        root_objnum = int(root_ref.decode('ascii').split()[0])
        root = self._objects[root_objnum].dictionary
        filespec_objnum = gemini_objnum + 1
        filespec = GemdocPDFObject(
                    (f'{filespec_objnum} 0 obj\r'
                     '<</Type/Filespec'
                      f'/F({gemini_filename})'
                      f'/EF<</F {gemini_objnum} 0 R>>'
                    f'>>\nendobj\r').encode('ascii', errors='replace')
                   )
        self._objects[filespec_objnum] = filespec
        fileref = f'{gemini_objnum} 0 R'.encode('ascii')
        root[b'/Names'] = {b'/EmbeddedFiles': {b'/Names': [
                              f'({gemini_filename})'.encode('ascii',
                                                            errors='replace'),
                              f'{filespec_objnum} 0 R'.encode('ascii'),
                          ]}}
        new_size = str(filespec_objnum+1).encode('ascii')
        self._trailer.dictionary[b'/Size'] = new_size
    def _info_dict(self):
        info_ref = self._trailer.dictionary.get(b'/Info')
        info_objnum = int(info_ref.decode('ascii').split()[0])
        return self._objects[info_objnum].dictionary
    def set_metadata(self, metadata: dict):
        info = self._info_dict()
        for k in list(info.keys()):
            if info[k] == b'()': info.pop(k)
        for k, v in metadata.items():
            if   k == 'author':    k = b'/Author'
            elif k == 'title':     k = b'/Title'
            elif k == 'date':      k = b'/PublishingDate'
            elif k == 'url':       k = b'/URL'
            elif k == 'subject':   k = b'/Subject'
            elif k == 'keywords':  k = b'/Keywords'
            info[k] = f'({v})'.encode('ascii')
    def get_metadata(self):
        metadata = dict()
        for k, v in self._info_dict().items():
            if   k == b'/Author':          k = 'author'
            elif k == b'/Title':           k = 'title'
            elif k == b'/PublishingDate':  k = 'date'
            elif k == b'/URL':             k = 'url'
            elif k == b'/Subject':         k = 'subject'
            elif k == b'/Keywords':        k = 'keywords'
            else: continue
            if not (v.startswith(b'(') and v.endswith(b')')): continue
            metadata[k] = v[1:-1].decode('ascii')
        return metadata
    def serialize(self) -> bytes:
        xref = dict()
        if self._gemini != None:
            result = f'%PDF-1.6\n{magic_line}\n```\n```\r'.encode('utf-8')
            xref[self._gemini_objnum] = len(result)
            gemini_length = len(self._gemini.encode('utf-8'))
            result += (f'{self._gemini_objnum} 0 obj\r'
                        '<</Type/EmbeddedFile/Params'
                            f'<</Size {gemini_length+1}>>'
                         f'/Length {gemini_length+1}>>\rstream\n'
                       f'{self._gemini}\n\nendstream\nendobj\n') \
                                                            .encode('utf-8')
        else:
            result = f'%PDF-1.6\n%¶🗎\ufe0e\n'.encode('utf-8')
        result += b'```% What follows is a pdf representation of this file\n'
        for objnum, obj in self._objects.items():
            xref[objnum] = len(result)
            result += obj.serialize()
        startxref = len(result); result += b'xref\r'
        result += f'0 {max(xref.keys())+1}\r'.encode('ascii')
        result += (10*'0'+' 65535 f \r').encode('ascii')
        last_free = 0
        for i in range(1, max(xref.keys())+1):
            if i in xref:
                result += f'{xref[i]:010d} 00000 n \r'.encode('ascii')
            else:
                result += f'{last_free:010d} 00001 f \r'.encode('ascii')
                last_free = i
        result += self._trailer.serialize()
        result += f'startxref\r{startxref}\r%%EOF\n'.encode('ascii')
        return result


class GemdocParserException(Exception):
    pass

def is_gemdoc_pdf(doc: str) -> bool:
    """
    Note that this function throws a GemdocParserException if it receives
    a pdf file that does not contain a valid gemdoc signature on the second
    line.
    """
    if not doc.lstrip().startswith('%PDF-'):
        False
    elif not doc.lstrip().splitlines()[1].startswith(magic_line):
        raise GemdocParserException(
            'Received a pdf file but the gemdoc signature of '
           f"'{magic_line}' on the second line is missing."
        )
    else:
        return True

def extract_gemini_part(doc: str) -> tuple[str,dict]:
    metadata = GemdocPDF(None, doc.encode('utf-8')).get_metadata()
    start = doc.index('stream\n') + 7
    end = doc.index('\nendstream\nendobj\n', start)
    doc = doc[start:end]
    # strip a single additional newline added in by gemdoc itself
    if doc.endswith('\n'): doc = doc[:-1]
    return doc, metadata

def parse_magic_lines(doc: str) -> tuple[str,dict]:
    metadata = dict(); body = list()
    for line in doc.splitlines():
        if line.startswith('%!GEMDOC'):
            key, *value = line[8:].lstrip().split('=', maxsplit=1)
            if not value: value.append('')
            key, value = key.strip().lower(), value[0].strip()
            if key == 'uri': key = 'url'
            if key not in ['author', 'date', 'url', 'subject', 'keywords']:
                raise GemdocParserException(f"Unsupported gemdoc key '{key}'")
            metadata[key] = value
        else:
            body.append(line)
    return '\n'.join(body), metadata

def parse_gemini(doc: str, metadata: dict) -> tuple[str,str]:
    body = list(); got_title = False; preformatted = False
    def add(line, tag='p', css_class=None) -> None:
        if tag and css_class:
            body.append(f'<{tag} class="{css_class}">'
                        f'{html_escape(line)}</{tag}>')
        elif tag:
            body.append(f'<{tag}>{html_escape(line)}</{tag}>')
        else:
            body.append(html_escape(line))
    doc = doc.splitlines(); i = 0
    while i < len(doc):
        if preformatted and doc[i].startswith('```'):
            body.append('</pre>'); preformatted = False
        elif preformatted:
            add(doc[i], tag=None)
        elif doc[i].startswith('```'):
            body.append('<pre>'); preformatted = True
        elif doc[i].startswith('# '):
            body.append('<div class="headingcontext">')
            if not got_title:
                got_title = True; title = doc[i][2:].strip()
                add(title, tag='h1', css_class='title')
                if doc[i+1].startswith('## '):
                    i += 1; subtitle = doc[i][3:].strip()
                    add(subtitle, tag='h2', css_class='subtitle')
                else:
                    subtitle = None
                if title and subtitle and title[-1] in '.,;:?!':
                    metadata['title'] = f'{title} {subtitle}'
                elif title and subtitle:
                    metadata['title'] = f'{title}: {subtitle}'
                elif title:
                    metadata['title'] = title
                metadata['title'] = ''.join((c if c.isascii() else '_'
                                             for c in metadata['title']))
            else:
                add(doc[i][2:], tag='h1')
            i += 1
            while i < len(doc) and not doc[i].strip():
                body.append('<br />'); i += 1
            i -= 1
            body.append('</div>')
        elif doc[i].startswith('## '):
            body.append('<div class="headingcontext">')
            add(doc[i][3:], tag='h2')
            i += 1
            while i < len(doc) and not doc[i].strip():
                body.append('<br />'); i += 1
            i -= 1
            body.append('</div>')
        elif doc[i].startswith('### '):
            body.append('<div class="headingcontext">')
            add(doc[i][4:], tag='h3')
            i += 1
            while i < len(doc) and not doc[i].strip():
                body.append('<br />'); i += 1
            i -= 1
            body.append('</div>')
        elif doc[i].startswith('>'):
            add(doc[i][1:], tag='blockquote')
        elif doc[i].startswith('* '):
            body.append('<ul>')
            while i < len(doc) and doc[i].startswith('* '):
                add(doc[i][2:], tag='li')
                i += 1
            i -= 1; body.append('</ul>')
        elif doc[i].startswith('=>'):
            link, *label = doc[i][2:].lstrip().split(maxsplit=1)
            if 'url' not in metadata and link.startswith('//'):
                link = 'gemini:' + link
                doc[i] = f'=> {link}{"  " if label else ""}{label}'
            scheme, *_= urlparse(link)
            if 'url' in metadata and not scheme:
                base = metadata['url']
                if base.startswith('gemini://'):
                    # Work around missing IANA registration of gemini://
                    link = 'gemini:' + urljoin(base[7:], link)
                else:
                    link = urljoin(metadata['url'], link)
                scheme, *_= urlparse(link)
                doc[i] = f'=> {link}{"  " if label else ""}{label}'
            css_class = scheme
            label = label[0] if label else html_escape(link)
            if link == label:
                css_class += (' ' if css_class else '') + '_nolabel'
            body.append(f'<p><a href="{link}" class="{css_class}">'
                        f'{html_escape(label)}</a></p>')
        elif not doc[i].strip():
            body.append('<br />')
        else:
            add(doc[i])
        i += 1
    # Try to automatically extract author and date from url if they are
    # missing from metadata
    if 'url' in metadata and metadata['url'] and \
                ('author' not in metadata or 'date' not in metadata):
        _scheme, _netloc, path, *_ = urlparse(metadata['url'])
        if 'author' not in metadata and path.startswith('/~'):
            metadata['author'] = path[2:].split('/')[0]
        if 'date' not in metadata:
            possible_date = re.match(
                r'^([0-9]{4})[-/]?([0-9]{2})[-/]?([0-9]{2})([^0-9].*)$',
                path.split('/')[-1]
            )
            if possible_date:
                yyyy, mm, dd, _ = possible_date.groups()
                metadata['date'] = f'{yyyy}-{mm}-{dd}'
    colophon = ''
    if metadata.get('author'):
        colophon += '<author>{}</author>' \
                                    .format(html_escape(metadata['author']))
    if metadata.get('date'):
        if colophon: colophon += '<datesep>, </datesep>'
        colophon += '<date>{}</date>'.format(html_escape(metadata['date']))
    if metadata.get('url'):
        if colophon: colophon += '<urlsep><br /></urlsep>'
        colophon += '<url><a href={}>{}</a></url>' \
                    .format(metadata['url'], html_escape(metadata['url']))
    gemini = '\n'.join(doc)
    html = ('<html><head>\n'
           f'<colophon>{colophon}</colophon>\n'
            '</head><body>\n'
            ''+'\n'.join(body)+'\n'
            '</body></html>')
    return gemini, html

_default_css = """
/* This style is based on Ayu Light from the amfora contrib/themes
   directory available at https://github.com/makew0rld/amfora/ */

/*** Text ***/
body {
    /* General settings such as the main font to use */
    font-family: DejaVu Sans, sans serif;
    text-align: justify;
}
p {
    /* Settings for paragraphs; i. e. for anything that is not a heading,
       a list, a blockquote, or a block of preformatted text. Note that
       links are also wrapped in 'p' tags, so the settings specified
       here also apply to those if they are not overridden further
       below. */
    color: #5c6166;

    /* Note that a single <br /> tag is inserted for every blank line
       in the text/gemini source file. This should be taken into account
       when specifying margins. */
    margin: 0;
}

/*** Links ***/
a {
    /* Default styling for links */
    text-decoration: none;
    color: #a37acc;
}
a::after {
    /* Insert the url in brackets after the link label */
    content: ' ('attr(href)')';
}
/* The _nolabel class describes links where no human-readable label is
   provided. In these cases, the content and the href of the a tag are
   the same. In order to not print the same url twice, the automated
   printing of the parenthesized url is disabled for those links. */
a._nolabel::before { content: ''; }
a._nolabel::after { content: ''; }

a.gemini {
    /* Styling for links to gemini:// urls */
    color: #399ee6;
}
a.gopher {
    /* Styling for links to gopher:// urls */
}
a.mailto {
    /* Styling for links to mailto: urls */
}
/* Note that these selectors work for any kind of url scheme. There is no
   need to define special rules for every scheme, though, since the default
   style defined above will be applied to all urls with schemes that aren't
   explicitly mentioned in the css file. */

/*** Headings ***/
h1 {
    color: #fa8d3e;
    margin: 0;
    text-align: left;
}
h1::before { content: '# '; }
h2 {
    color: #f2ae49;
    margin: 0;
    text-align: left;
}
h2::before { content: '## '; }
h3 {
    color: #f2ae49;
    margin: 0;
    text-align: left;
}
h3::before { content: '### '; }

h1.title {
    /* The first heading that serves as a document title */
}
h2.subtitle {
    /* The heading directly beneath the document title that serves as
       the document subtitle */
}

div.headingcontext {
    page-break-inside: avoid;
    page-break-after: avoid;
}

/*** Lists ***/
ul {
    color: #5c6166;
    margin: 0;
    padding-left: .8em;
}
li {
    margin: 0;
}

/*** Blockquotes ***/

blockquote {
    color: #4cbf99;
    margin: 0;
}

/*** Preformatted text ***/

pre {
    font-family: DejaVu Sans Mono, monospace;
    color: #86b300;
    page-break-inside: avoid;
    margin: 0;
}

/*** Colophon with additional information ***/

colophon {
    font-size: 10pt;
    color: #5c6166;
}
colophon > url > a {
    /* Undo default link styling in colophon */
    font-size: 10pt;
    color: #5c6166;
}
colophon > url > a::before { content: ''; }
colophon > url > a::after { content: ''; }

/*** Move the colophon into the page footer ***/

/* Note that a simpler but less customizable example for moving
   the colophon into the page footer is provided below */

colophon > author  { position: running(author);  }
colophon > datesep { position: running(datesep); }
colophon > date    { position: running(date);    }
colophon > urlsep  { position: running(urlsep);  }
colophon > url     { position: running(url);     }
@page:first {
    @bottom-right {
        content: element(author)
                 element(datesep)   /* The string ', ' if both author
                                       and date are specified. If either
                                       author or date are missing, this
                                       element is missing as well. */
                 element(date)
                 element(urlsep)    /* A single <br /> tag if either author
                                       or date are specified and if the url
                                       is specified as well. If the url is
                                       missing or if both author and date
                                       are missing, this element is missing
                                       as well. */
                 element(url)   ;
    }
}

/* If you want to use the default footer layout, you can also use
   the following code instead of the more involved example provided
   above. Make sure to remove the example above if you uncomment the
   one below. */
/*
colophon {
    position: running(footer);
}
@page:first {
    @bottom-right {
        content: element(footer);
    }
}
*/
""".lstrip()


_cli_help = """
Usage: gemdoc [OPTION]... FILE

Options
  -o FILE, --output=FILE    Write output to FILE rather than to stdout.
  -i, --in-place            Modify the input file in place. Or more
                            specifically, replace the input file with the
                            resulting polyglot file. If the input file is
                            already a polyglot file, this will simply update
                            the pdf part of that file to match the contents
                            of the text/gemini part.
  --no-convert              Do not convert the text/gemini file into a binary
                            polyglot. This may be useful to simply download
                            text/gemini files from gemini servers. It also
                            comes in handy when one wants to debug input from
                            a remote source that cannot be converted to pdf.
  -M K=V, --metadata=K=V    Set the metadata key K to value V. Valid keys
                            are 'author', 'date', 'url', 'subject' and
                            'keywords'. This option may be passed multiple
                            times to set more than one key.
                            For local input files, metadata may optionally
                            also be set by including lines like the following
                            one in the input document: '%!GEMDOC KEY=VALUE'.
                            The supported keys are the same as available via
                            the command line option. If a value is specified
                            via both options, the one passed via the command
                            line takes precedence. If neither are present and
                            the input is already in polyglot format, existing
                            pdf metadata will be preserved.
  --css FILE                Use the specified css stylesheet to style the
                            document. This option may be passed multiple
                            times to use multiple css files. User-specified
                            stylesheets are always applied on top of the
                            default stylesheet built into gemdoc itself.
                            This provides a convenient way to only adjust
                            parts of that stylesheet. If you want to not
                            use the default stylesheet at all, make sure
                            to override all the selectors present in that
                            stylesheet.
  --print-default-css       Print the default stylesheet to stdout or to
                            the file specified via --output.
  -h, --help                Print this help message and exit.
""".lstrip()

if __name__ == "__main__":
    opts, args = getopt(sys.argv[1:], 'ho:M:i',
                        ['help', 'output=', 'metadata=', 'css=',
                         'print-default-css', 'in-place', 'no-convert'])
    output = '-'; metadata = dict(); input_type = None
    in_place = False; o_flag = False; no_convert = False
    print_default_css = False; stylesheets = list()
    for k, v in opts:
        if k in ['-h', '--help']:
            print(_cli_help); exit(0)
        elif k in ['-o', '--output']:
            output = v; o_flag = True
        elif k in ['-i', '--in-place']:
            in_place = True
        elif k == '--no-convert':
            no_convert = True
        elif k in ['-M', '--metadata']:
            m_key, m_value = v.split('=', maxsplit=1) if '=' in v \
                             else v.split(':', maxsplit=1) if ':' in v \
                             else (v, '')
            m_key, m_value = m_key.strip(), m_value.strip()
            if m_key == 'uri': m_key = 'url'
            metadata[m_key] = m_value
        elif k == '--css':
            stylesheets.append(v)
        elif k == '--print-default-css':
            if args:
                print('The --print-default-css option cannot be combined '
                      'with positional arguments', file=sys.stderr)
                exit(1)
            print_default_css = True

    def write_output(doc: Union[str,bytes]):
        if output == '-':
            if type(doc) == str:
                sys.stdout.write(doc)
            elif type(doc) == bytes:
                sys.stdout.buffer.write(doc)
            else:
                raise Exception(f'Invalid type {type(doc)}')
        else:
            if type(doc) == str:
                with open(output, 'w') as f:
                    f.write(doc)
            elif type(doc) == bytes:
                with open(output, 'wb') as f:
                    f.write(doc)
            else:
                raise Exception(f'Invalid type {type(doc)}')

    if print_default_css:
        write_output(_default_css); exit(0)
    elif len(args) != 1:
        print('Gemdoc takes exactly one positional argument but got '
             f'{len(args)}. To force reading data from stdin, specify '
              'a single dash \'-\' as the input file.', file=sys.stderr)
        exit(1)
    elif args[0] == '-':
        doc = sys.stdin.read(); input_type = 'local'
    elif not args[0].startswith('gemini://') and os.path.exists(args[0]):
        with open(args[0]) as f:
            doc = f.read(); input_type = 'local'
    elif args[0].startswith('gemini://') or \
         re.match(r'^(//)?[^/\.]+\.[^/\.]+', args[0]):
        if args[0].startswith('//'): args[0] = 'gemini:'+args[0]
        if not args[0].startswith('gemini://'): args[0] = 'gemini://'+args[0]
        url, mime_type, doc = retrieve_url(args[0]); input_type = 'remote'
        if 'url' not in metadata: metadata['url'] = url
    else:
        print(f"'{args[0]}' does not seem to be a gemini url and there is "
               'no such file on the local system either.', file=sys.stderr)
        exit(1)

    if in_place:
        if o_flag:
            print('The -o and -i flags are mutually exclusive',
                  file=sys.stderr)
            exit(1)
        elif input_type != 'local':
            print('The -i flag can only be used for local inputs',
                  file=sys.stderr)
            exit(1)
        elif no_convert:
            print('The -i flag cannot be combined with the --no-convert '
                  'option', file=sys.stderr)
            exit(1)
        elif not os.path.isfile(args[0]) or os.path.islink(args[0]):
            print(f'Cannot modify \'{args[0]}\' in place: Not a regular '
                   'file', file=sys.stderr)
            exit(1)
        else:
            output = tempfile.mktemp(
                dir = os.path.dirname(args[0]),
                prefix = os.path.basename(args[0])+'.',
            )

    from weasyprint import HTML, CSS
    css = [CSS(string=_default_css)]
    try:
        for s in stylesheets:
            with open(s) as f:
                css.append(CSS(string=f.read()))
    except Exception as e:
        print(f'Unable to read css file. {e}', file=sys.stderr)
        exit(1)

    if input_type == 'local':
        if is_gemdoc_pdf(doc):
            doc, pdf_metadata = extract_gemini_part(doc)
        else:
            pdf_metadata = dict()
        doc, new_metadata = parse_magic_lines(doc)
        for k, v in new_metadata:
            if k not in metadata: metadata[k] = v
        for k, v in pdf_metadata.items():
            if k not in metadata: metadata[k] = v

    elif input_type == 'remote':
        if no_convert:
            write_output(doc)
            exit(0)
        elif mime_type.lower() in ['text/gemini', 'application/pdf'] \
                               and doc.lstrip().startswith('%PDF-'):
            write_output(doc)
            exit(0)
        elif mime_type.lower() == 'text/gemini':
            pass
        else:
            if not re.search(r'[^\.]\.[^\.]+$', output):
                output += guess_extension(mime_type, strict=False) or ''
            print(f'Writing non pdf file to {output}. The file\'s mime type '
                  f'was reported to be \'{mime_type}\'', file=sys.stderr)
            write_output(doc)
            exit(0)

    # Ensure that all metadata is valid ascii; possibly dropping characters
    for k, v in metadata.items():
        if k == 'url':
            no_urlquote_chars = '~:/?#[]@!$&\'()*+,;=%'
            v = urlquote(v, safe=no_urlquote_chars)
            v = urlquote(v, safe=no_urlquote_chars)
            # I believe that this invocation of the urlquote function
            # should be idempotent. That is why I apply it again when
            # writing pdf metadata; and yet again every time the pdf part
            # is updated. If this function should not be idempotent,
            # calling it twice early on should help me spot errors
            # earlier in the process.
            if v != metadata[k]:
                print(f'Warning: Non-ascii characters in the url field have '
                       'been escaped by percent-encoding them',
                      file=sys.stderr)
        else:
            v = ''.join((c if c.isascii() else '_' for c in v))
            if v != metadata[k]:
                print(f'Warning: Non-unicode characters in the {k} field '
                       'have been replaced with underscores',
                      file=sys.stderr)
        _ = v.encode('ascii') # Raise exception if encoding as ascii fails
        metadata[k] = v
    gemini_filename = 'source.gmi'
    if 'url' in metadata:
        _scheme, _netloc, path, *_ = urlparse(metadata['url'])
        if path:
            gemini_filename = path.split('/')[-1]
            if '%' in gemini_filename:
                gemini_filename = urlunquote(gemini_filename)
                gemini_filename = ''.join((c if c.isascii() else '_'
                                             for c in gemini_filename))
                print(f'Warning: Non-unicode characters in the filename '
                       'for the embedded source file have been replaced '
                       'with underscores', file=sys.stderr)
                _ = gemini_filename.encode('ascii')
            if not re.search(r'[^\.]\.[^\.]', gemini_filename):
                gemini_filename = gemini_filename+'.gmi'

    if 'endstream' in doc:
        doc = doc.replace('endstream', 'e\u200bndstream')
        print('Warning: Occurrences of the \'endstream\' keyword have been '
              'escaped by inserting a zero width space after the first '
              'character', file=sys.stderr)
    if 'endobj' in doc:
        doc = doc.replace('endobj', 'e\u200bndobj')
        print('Warning: Occurrences of the \'endobj\' keyword have been '
              'escaped by inserting a zero width space after the first '
              'character', file=sys.stderr)

    gemini, html = parse_gemini(doc, metadata)
    html = HTML(string=html)
    pdf = BytesIO()
    html.write_pdf(pdf, stylesheets=css)

    pdf.seek(0); polyglot = GemdocPDF(gemini, pdf.read(),
                                      gemini_filename=gemini_filename)
    polyglot.set_metadata(metadata)
    write_output(polyglot.serialize())
    if in_place: os.rename(output, args[0])
