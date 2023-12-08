# Gemdoc – The missing gemini to pdf printer

Gemdoc is a command line script that can be used to download documents
hosted via gemini and save them as text/gemini+pdf binary polyglot
files. The format of the binary polyglots relies rather heavily on
the techniques proposed in the more recent issues of the lab6 zine
(hosted at lab6.com/2 and lab6.com/3 via both gemini and https). The
implementation of these techniques was developed independently, however,
and all bugs related to text/gemini+pdf polyglot files produced with
gemdoc are entirely my own fault.

## Usage

The easiest way to use gemdoc is to simply pass it a gemini url as its
first (and only) command line argument, like this:

    gemdoc gemini://geminiprotocol.net/docs/faq.gmi

This command would create a text/gemini+pdf polyglot named `faq.pdf`
in the current working directory. To explicitly specify the filename for
the output file, an `-o` option is available. Furthermore, gemdoc also
features an `-i` option for in-place conversion of files stored on the
local file system. This in-place conversion facility can be used both
to turn regular text/gemini files into text/gemini+pdf polyglots and to
change the pdf layout of an existing text/gemini+pdf polyglot file.

Internally, the text/gemini representation of the input file will first
be converted to a small subset of html that is then further processed by
weasyprint in order to create a pdf representation of the content. The
default (built-in) css file is inspired by the stylesheet behind
<https://gmi.skyjake.fi/lagrange/> (as of May 24, 2023). An alternative
stylesheet called `old-default.css` is included in this repository. To
use the alternative stylesheet, simply run

    gemdoc --css old-default.css gemini://...

The easiest way to create an entirely customized stylesheet is to export
the default stylesheet by running

    gemdoc --print-default-css > user.css

and then taking it from there. Once you have adjusted `user.css` to
your liking, simply pass it back in by specifying `--css user.css`
on the command line.

For a comprehensive description of all available command line options,
please refer to the output produced by running

    gemdoc --help

## Installing

As of now, gemdoc is a single python script. To install it, simply mark
it as executable and move or symlink it to a directory included in your
`PATH`. You may also want to rename the script from `gemdoc.py` to
`gemdoc`. The documentation also generally uses the latter name when
referring to the program. The dependencies listed below need to be
satisfied for gemdoc to work, of course.

## Dependencies

- python3
- weasyprint

## License

Gemdoc is made available under the terms of the GNU General Purpose
License, version 3 or later. A copy of that license is included in this
repository as `license.txt`.
