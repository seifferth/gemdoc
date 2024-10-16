#!/usr/bin/env python3

import sys, os, tempfile
import re, base64, zlib, textwrap
import socket, ssl
from typing import Union
from io import BytesIO
from hashlib import sha256
from pkg_resources import parse_version
#from weasyprint import HTML, CSS       # moved below to improve performance
                                        # if weasyprint is not used.
from urllib.parse import urlparse, urljoin, quote as urlquote,\
                                            unquote as urlunquote
from html import escape as html_escape
from mimetypes import guess_extension
from getopt import gnu_getopt as getopt
from copy import deepcopy


magic_line = '%♊\ufe0e🗎\ufe0e'


def warn(msg: str):
    print(textwrap.fill(msg), file=sys.stderr)
def err(msg: str):
    warn(msg); exit(1)


class GemdocClientException(Exception):
    pass

def retrieve_url(url: str, max_redirects=5) -> \
                                        tuple[str,str,Union[str,bytes]]:
    """
    Returns a tuple of type (url, content), where url is possibly
    different from the one supplied as an argument if there have been
    any redirects.
    """
    if max_redirects <= 0:
        raise GemdocClientException('Maximum number of redirects exceeded')
    url = url.replace('\r\n', '%0A').replace('\n', '%0A')
    scheme, host, path, params, query, _fragment = urlparse(url); port = 1965
    query = query.replace(' ', '%20')
    url = f'{scheme}://{host}{path or "/"}'\
          f'{";"+params if params else ""}{"?"+query if query else ""}'
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
                lastlen = len(response)
                response += ssock.recv(1029-len(response))
                if len(response) == lastlen: break
            if b'\r\n' not in response:
                raise GemdocClientException('Server response too long')
            header, rest = response.split(b'\r\n', maxsplit=1)
            header = header.decode('utf-8')
            if not header[:2].isnumeric():
                raise GemdocClientException('Invalid response from server')
            if header.startswith('3'):
                dest = header[3:]
                destscheme, desthost, *_ = urlparse(dest)
                if destscheme: pass
                elif dest.startswith('//'): dest = f'gemini:{dest}'
                elif dest.startswith('/'): dest = f'gemini://{host}{dest}'
                else: dest = 'gemini:'+urljoin(f'//{host}{path}', dest)
                warn(f"Following redirect to '{dest}'")
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
    def _consume_list(self, binary: bytes, delim=(b'[',b']')) -> \
                                                        tuple[bytes,dict]:
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
    def serialize(self, flateencode) -> bytes:
        dictionary = deepcopy(self.dictionary)
        flist = dictionary.pop(b'/Filter', [])
        if type(flist) == bytes: flist = [flist]
        if self._stream != None:
            stream = self._stream
            if flateencode: stream = zlib.compress(stream)
            stream = base64.a85encode(stream)+b'~>'     # TODO: breaks images
            # Space-stuff stream if it could be mistaken for a
            # gemini preformatting toggle off line
            if stream.startswith(b'```'): stream = b' '+stream
            binary = (b'\rstream\n' + stream + b'\rendstream\r')
            if flateencode: flist.insert(0, b'/FlateDecode')
            flist.insert(0, b'/ASCII85Decode')          # TODO: breaks images
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
            raise Exception('Object revisions not implemented. '\
                            'Unable to parse '+str(binary[:20]))
        objnum = int(main.decode('ascii'))
        endobj = binary.find(b'endobj')+len(b'endobj')
        if endobj == -1: raise Exception('Missing endobj keyword')
        return objnum, binary[:endobj]+b'\n', binary[endobj:]
    def _set_file_identifier(self):
        if self._gemini_hash == None:
            raise Exception('Unable to set primary ID for pdf document '
                            'without a text/gemini representation')
        elif self._binary_hash == None:
            raise Exception('Unable to set secondary ID for pdf document '
                            'without a pdf representation')
        pdf_id = f'[<{self._gemini_hash}><{self._binary_hash}>]'
        self._trailer.dictionary[b'/ID'] = pdf_id.encode('ascii')
    def __init__(self, gemini: str, binary: Union[bytes,str],
                 gemini_filename='source.gmi', flateencode_streams=False):
        if type(binary) == str: binary = binary.encode('utf-8')
        self._gemini_hash = sha256(gemini.encode('utf-8')).hexdigest() \
                                                if gemini != None else None
        self._binary_hash = sha256(binary).hexdigest() \
                                                if binary != None else None
        self._flateencode_streams = flateencode_streams
        self._gemini = gemini
        self._objects = dict()
        self._trailer = GemdocPDFTrailer(b'')
        while binary:
            if re.match(rb'[\r\n]*xref', binary):
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
    def _make_utf16_string(self, s: str) -> str:
        return '<feff'+''.join(('{:02x}'.format(b)
                                for b in s.encode('utf-16be')))+'>'
    def _make_attachment(self, gemini_objnum, gemini_filename):
        root_ref = self._trailer.dictionary.get(b'/Root')
        root_objnum = int(root_ref.decode('ascii').split()[0])
        root = self._objects[root_objnum].dictionary
        filespec_objnum = gemini_objnum + 1
        filespec = GemdocPDFObject(
                    (f'{filespec_objnum} 0 obj\r'
                     '<</Type/Filespec/AFRelationship/Source'
                      f'/F'+self._make_utf16_string(gemini_filename)+\
                      f'/UF'+self._make_utf16_string(gemini_filename)+\
                      f'/EF<</F {gemini_objnum} 0 R>>'
                    f'>>\nendobj\r').encode('ascii')
                   )
        self._objects[filespec_objnum] = filespec
        fileref = f'{gemini_objnum} 0 R'.encode('ascii')
        root[b'/Names'] = {b'/EmbeddedFiles': {b'/Names': [
                              self._make_utf16_string(gemini_filename) \
                                                        .encode('ascii'),
                              f'{filespec_objnum} 0 R'.encode('ascii'),
                          ]}}
        if b'/AF' not in root: root[b'/AF'] = list()
        root[b'/AF'].append(f'{filespec_objnum} 0 R'.encode('ascii'))
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
            info[k] = self._make_utf16_string(v).encode('ascii')
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
            if v.startswith(b'(') and v.endswith(b')'):
                metadata[k] = v[1:-1].decode('ascii')
            elif v.startswith(b'<') and v.endswith(b'>'):
                metadata[k] = bytes.fromhex(v[1:-1].decode('ascii')) \
                                                        .decode('utf-16')
            else:
                pass
        return metadata
    def serialize(self) -> bytes:
        xref = dict()
        self._info_dict()[b'/Creator'] = b'(gemdoc)'
        p = self._info_dict().pop(b'/Producer')
        p_note = ' (with gemdoc postprocessing)'
        if p.startswith(b'(') and p.endswith(b')'):
            self._info_dict()[b'/Producer'] = p[:-1] + \
                                            p_note.encode('ascii') + p[-1:]
        elif p.startswith(b'<') and p.endswith(b'>'):
            self._info_dict()[b'/Producer'] = p[:-1] + \
                   self._make_utf16_string(p_note).encode('ascii') + p[-1:]
        else:
            self._info_dict()[b'/Producer'] = p
        if self._gemini != None:
            self._set_file_identifier()
            result = f'%PDF-1.7\n{magic_line}\n```\n```\r'.encode('utf-8')
            xref[self._gemini_objnum] = len(result)
            gemini_length = len(self._gemini.encode('utf-8'))
            result += (f'{self._gemini_objnum} 0 obj\r'
                        '<</Type/EmbeddedFile/Subtype/text#2fgemini/Params'
                            f'<</Size {gemini_length+1}>>'
                         f'/Length {gemini_length+1}>>\rstream\n'
                       f'{self._gemini}\n\nendstream\nendobj\n') \
                                                            .encode('utf-8')
        else:
            result = f'%PDF-1.7\n%¶🗎\ufe0e\n'.encode('utf-8')
        result += b'```% What follows is a pdf representation of this file\n'
        for objnum, obj in self._objects.items():
            xref[objnum] = len(result)
            result += obj.serialize(flateencode=self._flateencode_streams)
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

def parse_gemini(doc: str, metadata: dict) -> tuple[str,str]:
    body = list(); got_title = False; preformatted = False
    _, site_host, *_ = urlparse(metadata.get('url', ''))
    def is_site_relative(link: str) -> bool:
        _, link_host, *_ = urlparse(link)
        return link_host == site_host
    def add(line, tag='p', css_class=None) -> None:
        if tag and css_class:
            body.append(f'<{tag} class="{css_class}">'
                        f'{html_escape(line)}</{tag}>')
        elif tag:
            body.append(f'<{tag}>{html_escape(line)}</{tag}>')
        else:
            body.append(html_escape(line))
    def add_empty_lines(i: int) -> int:
        """
        'i' is the index of the last, possibly non-empty line preceding
        the block of empty lines that shall be parsed. It is _not_ the
        index of the first empty line. The return value is the index of
        the last empty line, or the same as the input value if there are
        no empty lines. Note that this function appends empty lines to
        the html body.
        """
        i += 1
        while i < len(doc) and not doc[i].strip():
            body.append('<br />'); i += 1
        return i-1
    doc = doc.splitlines(); i = 0
    while i < len(doc):
        if preformatted and doc[i].startswith('```'):
            body.append('</pre>'); preformatted = False
            doc[i] = '```'
        elif preformatted:
            add(doc[i], tag=None)
        elif doc[i].startswith('```'):
            if i+1 < len(doc) and doc[i+1].startswith('```'):
                i += 1
            else:
                body.append('<pre>'); preformatted = True
        elif doc[i].startswith('###'):
            body.append('<div class="headingcontext">')
            add(doc[i][3:].strip(), tag='h3')
            i = add_empty_lines(i)
            body.append('</div>')
        elif doc[i].startswith('##'):
            body.append('<div class="headingcontext">')
            add(doc[i][2:].strip(), tag='h2')
            i = add_empty_lines(i)
            body.append('</div>')
        elif doc[i].startswith('#'):
            body.append('<div class="headingcontext">')
            if not got_title:
                got_title = True; title = doc[i][1:].strip()
                add(title, tag='h1', css_class='title')
                i = add_empty_lines(i)
                if i+1 < len(doc) and doc[i+1].startswith('##') \
                                  and doc[i+1][2:3] != '#':
                    i += 1; subtitle = doc[i][2:].strip()
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
                i = add_empty_lines(i)
            else:
                add(doc[i][2:], tag='h1')
                i = add_empty_lines(i)
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
            label = label[0] if label else ''
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
            if is_site_relative(link):
                css_class += (' ' if css_class else '') + '_internal'
            if not label:
                label = html_escape(link)
                css_class += (' ' if css_class else '') + '_nolabel'
            body.append(f'<a href="{link}" class="{css_class}"><p>'
                        f'<span class="label">{html_escape(label)}</span> '
                        f'<br /><span class="url">{html_escape(link)}</span>'
                         '</p></a>')
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
                r'^([0-9]{4})([-/_])?([0-9]{2})\2([0-9]{2})([^0-9].*)$',
                path.split('/')[-1]
            )
            if possible_date:
                yyyy, _sep, mm, dd, _ = possible_date.groups()
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
/* Loosely based on the stylesheet behind https://gmi.skyjake.fi/lagrange/ */

/*** Text ***/
html {
    /* Default background and foreground colour */
    background: #fff;
    color: rgb(26, 24, 0);
}
body {
    /* General settings such as the main font to use */
    font-family: Roboto, sans serif;
    font-weight: 400;
    font-size: 15pt;
    line-height: 140%;
    text-align: justify;
}
p {
    /* Settings for paragraphs; i. e. for anything that is not a heading,
       a list, a blockquote, or a block of preformatted text. Note that
       links are also wrapped in 'p' tags, so the settings specified
       here also apply to those if they are not overridden further
       below. */

    /* Note that a single <br /> tag is inserted for every blank line
       in the text/gemini source file. This should be taken into account
       when specifying margins. */
    margin: 0;
}

/*** Links ***/
a > p {
    /* Paragraphs containing links (i. e. a single link per paragraph) */
    margin-left: 20pt;
    text-align: left;
}
a > p > span.label {
    /* Default styling for link labels */
    font-weight: 600;
}
a > p::before {
    content: '🌐︎';
    margin-left: -20pt;
    display: inline-block;
    width: 20pt;
    color: rgb(210, 120, 10);
}
a > p > span.url {
    /* Default styling for printed urls */
    font-weight: 400;
}
/* To display the link and its label on the same line, uncomment the
   line below */
/* a > p > br { display: none; } */

a._internal > p::before {
    /* The _internal class describes links that lead to the same site
    that has been specified as the page footer */
    content: '➤';
}
/* The _nolabel class describes links where no human-readable label is
   provided. In these cases, the content and the href of the a tag are
   the same. In order to not print the same url twice, the automated
   printing of the parenthesized url is disabled for those links. */
a._nolabel > p > br { display: none; }
a._nolabel > p > span.url { display: none; }

a.gemini > p {
    /* Styling for links to gemini:// urls */
}
a.gemini > p::before {
    color: rgb(10, 110, 130);
}
a.gopher > p {
    /* Styling for links to gopher:// urls */
}
a.mailto > p {
    /* Styling for links to mailto: urls */
}
a.mailto > p::before {
    content: '🖂︎';
    color: rgb(10, 110, 130);
}
/* Note that these selectors work for any kind of url scheme. There is no
   need to define special rules for every scheme, though, since the default
   style defined above will be applied to all urls with schemes that aren't
   explicitly mentioned in the css file. */

/*** Headings ***/
h1 {
    font-size: 200%;
    font-weight: 700;
    color: rgb(160, 130, 0);
    line-height: 120%;
    margin-top: 1ex;
    margin-bottom: 1ex;
    text-align: left;
}
h2 {
    font-size: 167%;
    font-weight: 400;
    color: rgb(76, 122, 51);
    line-height: 120%;
    margin-top: 1ex;
    margin-bottom: 1ex;
    text-align: left;
}
h3 {
    font-size: 133%;
    font-weight: 700;
    color: rgb(0, 102, 102);
    margin: 0;
    text-align: left;
}
/* To show the octothorpes in front of headings, uncomment the following
   three lines */
/*
h1::before { content: '# '; }
h2::before { content: '## '; }
h3::before { content: '### '; }
*/

h1.title {
    /* The first heading that serves as a document title */
}
h2.subtitle {
    /* The heading directly beneath the document title that serves as
       the document subtitle */
    color: rgb(160, 130, 0);
}

/*** Lists ***/
ul {
    margin: 0;
    padding-left: 20pt;
    list-style: none;
}
li {
    margin: 0;
}
li::before {
    content: '•';
    color: #008080;
    font-weight: bold;
    display: inline-block;
    width: 16pt;
    margin-left: -16pt;
}

/*** Blockquotes ***/

blockquote {
    color: #008080;
    margin-top: 0;
    margin-bottom: 0;
    margin-left: 2.25em;
    font-style: italic;
    font-weight: 300;
    padding-left: 0.75em;
    border-left: 1px solid #597f7d;
}

/*** Preformatted text ***/

pre {
    font-family: Fira Mono, monospace;
    font-size: 90%;
    line-height: 110%;
    margin: 0;
    color: #008080;
    max-width: 100%;
    overflow: auto;
    page-break-inside: avoid;
}

/*** Colophon with additional information ***/

colophon {
    font-size: 80%;
    line-height: 110%;
    color: #806000;
}

/*** Move the colophon into the page footer ***/

/* Note that a simpler but less customizable example for moving
   the colophon into the page footer is provided below */

colophon > author  { position: running(author);  }
colophon > datesep { position: running(datesep); }
colophon > date    { position: running(date);    }
colophon > urlsep  { position: running(urlsep);  }
colophon > url     { position: running(url);     }
@page:first {
    margin-bottom: 2.5cm;
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

_minimal_css = """
a {
    color: inherit;
    text-decoration: none;
}
div.headingcontext {
    page-break-inside: avoid;
    page-break-after: avoid;
}
""".lstrip()


_cli_help = """
Usage: gemdoc [OPTION]... <GEMINI-URL|INPUT-FILE>

Options
  -o FILE, --output=FILE    Write output to FILE. To print output to stdout,
                            specify a single dash '-' as the output filename.
                            If no output file is specified, the filename will
                            be set automatically based on the source URL.
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
  -M K=V, --metadata=K=V    Set the metadata key K to value V. Valid keys are
                            'author', 'date', 'url', 'subject' and 'keywords'.
                            This option may be passed multiple times to set
                            more than one key. If the input is already in
                            polyglot format, existing pdf metadata will be
                            preserved.
  --css FILE                Use the specified css file to style the document.
                            This option may be passed multiple times to use
                            multiple stylesheets. If this option is supplied,
                            the default stylesheet will not be applied.
  --print-default-css       Print the default stylesheet to stdout or to the
                            file specified via --output.
  -h, --help                Print this help message and exit.
""".lstrip()

if __name__ == "__main__":
    opts, args = getopt(sys.argv[1:], 'ho:M:i',
                        ['help', 'output=', 'metadata=', 'css=',
                         'print-default-css', 'in-place', 'no-convert'])
    output = None; metadata = dict(); input_type = None
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
                err('The --print-default-css option cannot be combined '
                    'with positional arguments')
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
        if output == None: output = '-'
        write_output(_default_css); exit(0)
    elif len(args) != 1:
        err('Gemdoc takes exactly one positional argument but got '
           f'{len(args)}. To force reading data from stdin, specify '
            'a single dash \'-\' as the input file.')
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
        err(f"'{args[0]}' does not seem to be a gemini url and there is "
             'no such file on the local system either.')

    if no_convert and input_type == 'local':
        err('The --no-convert option can only be used with remote inputs')
    elif not o_flag and not in_place:
        if input_type == 'local':
            err('Either -i or -o must be specified for local inputs')
        else:
            pass  # The filename will be determined later on based on the URL
    elif in_place:
        if o_flag:
            err('The -o and -i flags are mutually exclusive')
        elif input_type != 'local':
            err('The -i flag can only be used for local inputs')
        elif args[0] == '-':
            err('The -i flag can not be used to process stdin. To use gemdoc'
                'as a unix filter, use \'-o-\' instead.')
        elif not os.path.isfile(args[0]) or os.path.islink(args[0]):
            err(f'Cannot modify \'{args[0]}\' in place: Not a regular file')
        else:
            output = tempfile.mktemp(
                dir = os.path.dirname(args[0]),
                prefix = os.path.basename(args[0])+'.',
            )

    from weasyprint import HTML, CSS, __version__ as weasyprint_version
    css = [CSS(string=_minimal_css)]
    try:
        for s in stylesheets:
            with open(s) as f:
                css.append(CSS(string=f.read()))
    except Exception as e:
        err(f'Unable to read css file. {e}')
    if not stylesheets: css.append(CSS(string=_default_css))

    if input_type == 'local':
        if is_gemdoc_pdf(doc):
            doc, pdf_metadata = extract_gemini_part(doc)
            for k, v in pdf_metadata.items():
                if k not in metadata: metadata[k] = v

    elif input_type == 'remote':
        if not o_flag:
            _, _, input_url_path, *_ = urlparse(args[0])
            output = os.path.basename(input_url_path.rstrip('/')) \
                                     .lstrip('.~/')
            if mime_type == 'text/gemini' and output.endswith('.gmi'):
                if not no_convert: output = output[:-4]+'.pdf'
            if not re.search(r'[^\.]\.[^\.]+$', output):
                if mime_type == 'text/gemini':
                    output += '.gmi' if no_convert else '.pdf'
                else:
                    output += guess_extension(mime_type, strict=False) or ''
            if os.path.exists(output):
                err(f'The output file \'{output}\' already exists. This file '
                    f'will not be replaced. To replace \'{output}\', use the '
                     '-o flag to explicitly specify the filename.')
        if no_convert:
            write_output(doc)
            exit(0)
        elif mime_type.lower() == 'text/gemini' \
                        and doc.lstrip().startswith('%PDF-') \
          or mime_type.lower() == 'application/pdf' \
                        and doc.lstrip().startswith(b'%PDF-'):
            write_output(doc)
            exit(0)
        elif mime_type.lower() == 'text/gemini':
            pass
        else:
            warn(f'Writing non pdf file to {output}. The file\'s mime type '
                 f'was reported to be \'{mime_type}\'.')
            write_output(doc)
            exit(0)

    gemini_filename = 'source.gmi'
    if 'url' in metadata:
        _scheme, _netloc, path, *_ = urlparse(metadata['url'])
        if path:
            gemini_filename = path.split('/')[-1]
            if '%' in gemini_filename:
                gemini_filename = urlunquote(gemini_filename)
            if not re.search(r'[^\.]\.[^\.]', gemini_filename):
                gemini_filename = gemini_filename+'.gmi'

    if 'endstream' in doc:
        doc = doc.replace('endstream', 'e\u200bndstream')
        warn('Warning: Occurrences of the \'endstream\' keyword have been '
             'escaped by inserting a zero width space after the first '
             'character')
    if 'endobj' in doc:
        doc = doc.replace('endobj', 'e\u200bndobj')
        warn('Warning: Occurrences of the \'endobj\' keyword have been '
             'escaped by inserting a zero width space after the first '
             'character')

    gemini, html = parse_gemini(doc, metadata)
    html = HTML(string=html)
    pdf = BytesIO()
    extra_weasyprint_opts = {}
    extra_gemdocpdf_opts = {}
    weasyprint_version = parse_version(weasyprint_version)
    if weasyprint_version < parse_version('56.0'):
        warn('The current version of weasyprint (version '
            f'{weasyprint_version}) does not include support for generating '
             'PDF/A documents. To have gemdoc generate a file that conforms '
             'to PDF/A requirements, make sure to use weasyprint version '
             '56.0 or above.')
    elif weasyprint_version < parse_version('59.0b1'):
        if weasyprint_version < parse_version('57.2'):
            warn('The current version of weasyprint (version '
                f'{weasyprint_version}) is known to generate pdfs that do '
                 'not fully conform to the PDF/A-3B specification. To have '
                 'gemdoc generate a file that fully conforms to PDF/A-3B '
                 'requirements, make sure to use weasyprint version 58 or '
                 'above.')
        extra_weasyprint_opts['version'] = '1.7'
        extra_weasyprint_opts['variant'] = 'pdf/a-3b'
    else:
        extra_weasyprint_opts['pdf_version'] = '1.7'
        extra_weasyprint_opts['pdf_variant'] = 'pdf/a-3b'
        extra_weasyprint_opts['uncompressed_pdf'] = True
        extra_gemdocpdf_opts['flateencode_streams'] = True

    html.write_pdf(pdf, stylesheets=css, **extra_weasyprint_opts)
    pdf.seek(0); polyglot = GemdocPDF(gemini, pdf.read(),
                                      gemini_filename=gemini_filename,
                                      **extra_gemdocpdf_opts)
    polyglot.set_metadata(metadata)
    write_output(polyglot.serialize())
    if in_place: os.rename(output, args[0])
