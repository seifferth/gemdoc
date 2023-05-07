#!/usr/bin/env python3

import sys, os
import re
import socket, ssl
from typing import Union
from io import BytesIO
#from weasyprint import HTML, CSS       # moved below to improve performance
                                        # if weasyprint is not used.
from urllib.parse import urlparse, urljoin
from html import escape as html_escape
from mimetypes import guess_extension
from getopt import gnu_getopt as getopt


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


class GemdocParserException(Exception):
    pass

def is_gemdoc_pdf(doc: str) -> bool:
    """
    Note that this function throws a GemdocParserException if it receives
    a pdf file that does not contain a valid gemdoc signature on the second
    line.
    """
    magic_line = '%♊\ufe0e🗎\ufe0e'
    if not doc.lstrip().startswith('%PDF-'):
        False
    elif not doc.lstrip().splitlines()[1].startswith(magic_line):
        raise GemdocParserException(
            'Received a pdf file but the gemdoc signature of '
           f"'{magic_line}' on the second line is missing."
        )
    else:
        return True

def extract_gemini_part(doc: str) -> str:
    start = doc.index('stream\n') + 7
    end = doc.index('\nendstream\nendobj\n', start)
    doc = doc[start:end]
    # strip a single additional newline added in by gemdoc itself
    if doc.endswith('\n'): doc = doc[:-1]
    return doc

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
            else:
                add(doc[i][2:], tag='h1')
        elif doc[i].startswith('## '):
            add(doc[i][3:], tag='h2')
        elif doc[i].startswith('### '):
            add(doc[i][4:], tag='h3')
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
  -M K=V, --metadata=K=V    Set the metadata key K to value V. Valid keys
                            are 'author', 'date', 'url', 'subject' and
                            'keywords'. This option may be passed multiple
                            times to set more than one key.
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
    opts, args = getopt(sys.argv[1:], 'ho:M:',
                        ['help', 'output=', 'metadata=', 'css=',
                         'print-default-css'])
    output = '-'; metadata = dict(); input_type = None
    print_default_css = False; stylesheets = list()
    for k, v in opts:
        if k in ['-h', '--help']:
            print(_cli_help); exit(0)
        elif k in ['-o', '--output']:
            output = v
        elif k in ['-M', '--metadata']:
            m_key, m_value = v.split('=', maxsplit=1) if '=' in v \
                             else v.split(':', maxsplit=1) if ':' in v \
                             else (v, '')
            m_key, m_value = m_key.strip(), m_value.strip()
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
    elif not args[0].startswith('gemini://') and os.path.isfile(args[0]):
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
        if is_gemdoc_pdf(doc): doc = extract_gemini_part(doc)
        doc, new_metadata = parse_magic_lines(doc)
        for k, v in new_metadata:
            if k not in metadata: metadata[k] = v

    elif input_type == 'remote':
        if mime_type.lower() in ['text/gemini', 'application/pdf'] \
                             and doc.lstrip().startswith('%PDF-'):
            write_output(doc)
            exit(0)
        elif mime_type.lower() == 'text/gemini':
            pass
        else:
            if not re.match(r'[^\.]\.[^\.]+$', output):
                output += guess_extension(mime_type, strict=False) or ''
            print(f'Writing non pdf file to {output}. The file\'s mime type '
                  f'was reported to be \'{mime_type}\'', file=sys.stderr)
            write_output(doc)
            exit(0)

    gemini, html = parse_gemini(doc, metadata)
    html = HTML(string=html)
    pdf = BytesIO()
    html.write_pdf(pdf, stylesheets=css)
    pdf.seek(0); write_output(pdf.read())
