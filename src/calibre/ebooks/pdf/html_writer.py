#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2019, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import absolute_import, division, print_function, unicode_literals

import json
import os
import signal
from collections import namedtuple
from io import BytesIO

from PyQt5.Qt import QApplication, QMarginsF, QPageLayout, QTimer, QUrl
from PyQt5.QtWebEngineWidgets import QWebEnginePage

from calibre.constants import iswindows
from calibre.ebooks.metadata.xmp import metadata_to_xmp_packet
from calibre.ebooks.oeb.base import XHTML
from calibre.ebooks.oeb.polish.container import Container as ContainerBase
from calibre.ebooks.oeb.polish.split import merge_html
from calibre.ebooks.oeb.polish.toc import get_toc
from calibre.ebooks.pdf.image_writer import (
    Image, PDFMetadata, draw_image_page, get_page_layout
)
from calibre.ebooks.pdf.render.serialize import PDFStream
from calibre.gui2 import setup_unix_signals
from calibre.gui2.webengine import secure_webengine
from calibre.utils.logging import default_log
from calibre.utils.podofo import get_podofo, set_metadata_implementation
from calibre.utils.short_uuid import uuid4
from polyglot.builtins import iteritems, range

OK, LOAD_FAILED, KILL_SIGNAL = range(0, 3)


class Container(ContainerBase):

    tweak_mode = True
    is_dir = True

    def __init__(self, opf_path, log, root_dir=None):
        ContainerBase.__init__(self, root_dir or os.path.dirname(opf_path), opf_path, log)


class Renderer(QWebEnginePage):

    def __init__(self, opts):
        QWebEnginePage.__init__(self)
        secure_webengine(self)
        self.settle_time = 0
        s = self.settings()
        s.setAttribute(s.JavascriptEnabled, True)
        s.setFontSize(s.DefaultFontSize, opts.pdf_default_font_size)
        s.setFontSize(s.DefaultFixedFontSize, opts.pdf_mono_font_size)
        s.setFontSize(s.MinimumLogicalFontSize, 8)
        s.setFontSize(s.MinimumFontSize, 8)
        std = {
            'serif': opts.pdf_serif_family,
            'sans' : opts.pdf_sans_family,
            'mono' : opts.pdf_mono_family
        }.get(opts.pdf_standard_font, opts.pdf_serif_family)
        if std:
            s.setFontFamily(s.StandardFont, std)
        if opts.pdf_serif_family:
            s.setFontFamily(s.SerifFont, opts.pdf_serif_family)
        if opts.pdf_sans_family:
            s.setFontFamily(s.SansSerifFont, opts.pdf_sans_family)
        if opts.pdf_mono_family:
            s.setFontFamily(s.FixedFont, opts.pdf_mono_family)

        self.loadFinished.connect(self.load_finished)
        if not iswindows:
            self.original_signal_handlers = setup_unix_signals(self)

    def block_signal_handlers(self):
        for sig in self.original_signal_handlers:
            signal.signal(sig, lambda x, y: None)

    def restore_signal_handlers(self):
        for sig, handler in self.original_signal_handlers.items():
            signal.signal(sig, handler)

    def load_finished(self, ok):
        if not ok:
            QApplication.instance().exit(LOAD_FAILED)
            return
        QTimer.singleShot(int(1000 * self.settle_time), self.print_to_pdf)

    def signal_received(self, read_fd):
        try:
            os.read(read_fd, 1024)
        except EnvironmentError:
            return
        QApplication.instance().exit(KILL_SIGNAL)

    def print_to_pdf(self):
        self.printToPdf(self.printing_done, self.page_layout)

    def printing_done(self, pdf_data):
        self.pdf_data = pdf_data
        QApplication.instance().exit(OK)

    def run_loop(self):
        self.block_signal_handlers()
        try:
            return QApplication.exec_()
        finally:
            self.restore_signal_handlers()

    def convert_html_file(self, path, page_layout, settle_time=0):
        self.settle_time = settle_time
        self.page_layout = page_layout
        self.pdf_data = None
        self.setUrl(QUrl.fromLocalFile(path))
        ret = self.run_loop()
        if ret == LOAD_FAILED:
            raise SystemExit('Failed to load {}'.format(path))
        if ret == KILL_SIGNAL:
            raise SystemExit('Kill signal received')
        if ret != OK:
            raise SystemExit('Unknown error occurred')
        return self.pdf_data


def update_metadata(pdf_doc, pdf_metadata):
    if pdf_metadata.mi:
        xmp_packet = metadata_to_xmp_packet(pdf_metadata.mi)
        set_metadata_implementation(
            pdf_doc, pdf_metadata.title, pdf_metadata.mi.authors,
            pdf_metadata.mi.book_producer, pdf_metadata.mi.tags, xmp_packet)


def add_cover(pdf_doc, cover_data, page_layout, opts):
    buf = BytesIO()
    page_size = page_layout.fullRectPoints().size()
    img = Image(cover_data)
    writer = PDFStream(buf, (page_size.width(), page_size.height()), compress=True)
    writer.apply_fill(color=(1, 1, 1))
    draw_image_page(writer, img, preserve_aspect_ratio=opts.preserve_cover_aspect_ratio)
    writer.end()
    cover_pdf = buf.getvalue()
    podofo = get_podofo()
    cover_pdf_doc = podofo.PDFDoc()
    cover_pdf_doc.load(cover_pdf)
    pdf_doc.insert_existing_page(cover_pdf_doc)


def create_margin_groups(container):

    def merge_group(group):
        if len(group) > 1:
            group_margins = group[0][1]
            names = [name for (name, margins) in group]
            merge_html(container, names, names[0], insert_page_breaks=True)
            group = [(names[0], group_margins)]
        return group

    groups = []
    current_group = []
    for name, is_linear in container.spine_names:
        root = container.parsed(name)
        margins = root.get('data-calibre-pdf-output-page-margins')
        if margins:
            margins = json.loads(margins)
        if current_group:
            prev_margins = current_group[-1][1]
            if prev_margins != margins:
                groups.append(merge_group(current_group))
                current_group = []
        current_group.append((name, margins))
    if current_group:
        groups.append(merge_group(current_group))
    return groups


def render_name(container, name, margins, renderer, page_layout):
    index_file = container.name_to_abspath(name)
    if margins:
        page_layout = QPageLayout(page_layout)
        page_layout.setUnits(QPageLayout.Point)
        old_margins = page_layout.marginsPoints()
        new_margins = QMarginsF(
            margins.get('left', old_margins.left()),
            margins.get('top', old_margins.top()),
            margins.get('right', old_margins.right()),
            margins.get('bottom', old_margins.bottom()))
        page_layout.setMargins(new_margins)
    pdf_data = renderer.convert_html_file(index_file, page_layout, settle_time=1)
    podofo = get_podofo()
    pdf_doc = podofo.PDFDoc()
    pdf_doc.load(pdf_data)
    return pdf_doc


def add_anchors_markup(root, uuid, anchors):
    body = root[-1]
    div = body.makeelement(XHTML('div'), id=uuid, style='page-break-before: always')
    body.append(div)
    for i, anchor in enumerate(anchors):
        div.append(div.makeelement(XHTML('a'), href='#' + anchor))
        div[-1].text = '{}'.format(i)
        div[-1].tail = ' '
    div.append(div.makeelement(XHTML('a'), href='#' + uuid))
    div[-1].text = 'top'
    div[-1].tail = ' '


def add_toc_links(container, toc, margin_groups):
    # TODO: Change this to work for all anchors so it can be used to fix
    # arbitrary links
    uuid = uuid4()
    name_anchor_map = {}
    for item in toc.iterdescendants():
        if item.dest and item.frag:
            anchors = name_anchor_map.setdefault(item.dest, set())
            anchors.add(item.frag)
    for group in margin_groups:
        name = group[0][0]
        anchors = name_anchor_map.get(name, set())
        add_anchors_markup(container.parsed(name), uuid, anchors)
        container.dirty(name)
    return uuid


def make_anchors_unique(container):
    mapping = {}
    count = 0
    base = None

    def replacer(url):
        if not url:
            return url
        if '#' not in url:
            return url
        if url.startswith('#'):
            href, frag = base, url[1:]
        else:
            href, frag = url.partition('#')[::2]
        name = container.href_to_name(href, base)
        if not name:
            return url
        key = name, frag
        new_frag = mapping.get(key)
        if new_frag is None:
            return url
        replacer.replaced = True
        if url.startswith('#'):
            return '#' + new_frag
        return href + '#' + new_frag

    for spine_name, is_linear in container.spine_names:
        root = container.parsed(spine_name)
        for elem in root.xpath('//*[@id]'):
            count += 1
            key = spine_name, elem.get('id')
            if key not in mapping:
                new_id = mapping[key] = 'a{}'.format(count)
                elem.set('id', new_id)

    for name in container.mime_map:
        base = name
        replacer.replaced = False
        container.replace_links(name, replacer)


AnchorLocation = namedtuple('AnchorLocation', 'pagenum left top zoom')


def get_anchor_locations(pdf_doc, first_page_num, toc_uuid):
    ans = {}
    anchors = pdf_doc.extract_anchors()
    toc_pagenum = anchors.pop(toc_uuid)[0]
    for r in range(pdf_doc.page_count(), toc_pagenum - 1, -1):
        pdf_doc.delete_page(r - 1)
    for anchor, loc in iteritems(anchors):
        loc = list(loc)
        loc[0] += first_page_num - 1
        ans[anchor] = AnchorLocation(*loc)
    return ans


def convert(opf_path, opts, metadata=None, output_path=None, log=default_log, cover_data=None):
    container = Container(opf_path, log)
    make_anchors_unique(container)
    margin_groups = create_margin_groups(container)
    toc = get_toc(container)
    toc_uuid = add_toc_links(container, toc, margin_groups)
    container.commit()

    renderer = Renderer(opts)
    page_layout = get_page_layout(opts)
    pdf_doc = None
    anchor_locations = {}
    num_pages = 0
    for group in margin_groups:
        name, margins = group[0]
        doc = render_name(container, name, margins, renderer, page_layout)
        anchor_locations.update(get_anchor_locations(doc, num_pages + 1, toc_uuid))
        num_pages += doc.page_count()

        if pdf_doc is None:
            pdf_doc = doc
        else:
            pdf_doc.append(doc)

    # TODO: Fix links using anchor_locations

    if cover_data:
        add_cover(pdf_doc, cover_data, page_layout, opts)

    if metadata is not None:
        update_metadata(pdf_doc, PDFMetadata(metadata))

    # TODO: Remove unused fonts
    # TODO: Remove duplicate fonts
    # TODO: Subset and embed fonts before rendering PDF

    pdf_data = pdf_doc.write()
    if output_path is None:
        return pdf_data
    with open(output_path, 'wb') as f:
        f.write(pdf_data)