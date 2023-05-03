#!/usr/bin/env python3

import sys
from weasyprint import HTML, CSS
from html import escape as html_escape

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

def parse_gemini(doc: str) -> tuple[str,dict]:
    metadata = dict(); body = list()
    got_title = False; preformatted = False
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
        elif doc[i].startswith('%!GEMDOC'):
            key, value = doc[i][8:].lstrip().split('=', maxsplit=1)
            key, value = key.strip(), value.strip()
            if key == 'uri': key = 'url'
            if key not in ['author', 'date', 'url', 'subject', 'keywords']:
                raise GemdocParserException(f"Unsupported gemdoc key '{key}'")
            metadata[key] = value
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
            while doc[i].startswith('* '):
                add(doc[i][2:], tag='li')
                i += 1
            i -= 1; body.append('</ul>')
        elif doc[i].startswith('=>'):
            link, label = doc[i][2:].lstrip().split(maxsplit=1)
            # TODO: Use the document's url to convert all relative links
            # into absolute links. Since we don't know where the pdf will
            # end up --- because most people assume pdfs are portable,
            # so they will just move them around --- this seems like the
            # appropriate thing to do. It should also help with making
            # the scheme detection on the next line more robust.
            #
            # TODO: Add some sanitisation code here. I might end up
            # processing remote content with this function, and this
            # (i. e. link and scheme) is the only part of my generated
            # html that is not run through html_escape.
            #
            scheme, _ = link.split(':', maxsplit=1)
            body.append(f'<p><a href="{link}" class="{scheme}">'
                        f'{html_escape(label)}</a></p>')
        elif not doc[i].strip():
            body.append('<br />')
        else:
            add(doc[i])
        i += 1
    colophon = ''
    if metadata.get('author'):
        colophon += '<author>{}</author>'.format(metadata['author'])
    if metadata.get('date'):
        if colophon: colophon += '<datesep>, </datesep>'
        colophon += '<date>{}</date>'.format(metadata['date'])
    if metadata.get('url'):
        if colophon: colophon += '<urlsep><br /></urlsep>'
        colophon += '<url><a href={url}>{url}</a></url>' \
                                                .format(url=metadata['url'])
    return ('<html><head>\n'
           f'<colophon>{colophon}</colophon>\n'
            '</head><body>\n'
            ''+'\n'.join(body)+'\n'
            '</body></html>', metadata)

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
a::after{
    /* Insert the url in brackets after the link label */
    content: ' ('attr(href)')';
}
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
}
h1::before { content: '# '; }
h2 {
    color: #f2ae49;
    margin: 0;
}
h2::before { content: '## '; }
h3 {
    color: #f2ae49;
    margin: 0;
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
""".strip()

if __name__ == "__main__":
    indoc = sys.stdin.read()
    if is_gemdoc_pdf(indoc):
        indoc = extract_gemini_part(indoc)
    doc, metadata = parse_gemini(indoc)
    html = HTML(string=doc)
    css = CSS(string=_default_css)
    html.write_pdf(sys.stdout.buffer, stylesheets=[css])
