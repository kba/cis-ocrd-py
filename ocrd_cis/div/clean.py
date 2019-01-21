from __future__ import absolute_import
from ocrd.utils import getLogger, concat_padded, xywh_from_points, points_from_x0y0x1y1
from ocrd.model.ocrd_page import from_file, to_xml, TextEquivType, CoordsType, GlyphType, WordType
from ocrd import Processor, MIMETYPE_PAGE
from ocrd_cis import get_ocrd_tool
from ocrd.model.ocrd_page_generateds import parse, parsexml_, parsexmlstring_
from collections import defaultdict


class Clean(Processor):

    def __init__(self, *args, **kwargs):
        self.ocrd_tool = get_ocrd_tool()
        kwargs['ocrd_tool'] = self.ocrd_tool['tools']['ocrd-cis-clean']
        kwargs['version'] = self.ocrd_tool['version']
        self.input_file_grp = kwargs['input_file_grp']
        super(Clean, self).__init__(*args, **kwargs)
        self.log = getLogger('Clean')

    def process(self):
        """
        Performs the (text) recognition.
        """

        mainLevel = self.parameter['mainLevel']
        mainIndex = self.parameter['mainIndex']

        inputfiles = self.input_files
        for (n, input_file) in enumerate(self.input_files):

            alignurl = input_file.url
            pcgts = parse(alignurl, True)
            page = pcgts.get_Page()
            regions = page.get_TextRegion()

            pagecontent = ''
            for region in regions:
                regioncontent = ''

                lines = region.get_TextLine()
                for line in lines:
                    linecontent = ''

                    words = line.get_Word()
                    for word in words:
                        wordunicode = word.get_TextEquiv()[mainIndex].Unicode
                        word.add_TextEquiv(TextEquivType(Unicode=wordunicode))
                        linecontent += ' ' + wordunicode


                    line.add_TextEquiv(TextEquivType(Unicode=regioncontent))
                    regioncontent += '\n' + linecontent

                region.add_TextEquiv(TextEquivType(Unicode=regioncontent))
                pagecontent += '\n' + regioncontent

            page.add_TextEquiv(TextEquivType(Unicode=pagecontent))

            ID = concat_padded(self.output_file_grp, n)
            self.log.info('creating file id: %s, name: %s, file_grp: %s',
                          ID, input_file.basename, self.output_file_grp)
            # Use the input file's basename for the new file
            # this way the files retain the same basenames.
            out = self.workspace.add_file(
                ID=ID,
                file_grp=self.output_file_grp,
                basename=self.output_file_grp + '-' + input_file.basename,
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts),
            )
            self.log.info('created file %s', out)