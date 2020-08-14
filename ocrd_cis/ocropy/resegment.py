from __future__ import absolute_import

import os.path
import numpy as np
from skimage import draw
import cv2
from shapely.geometry import Polygon
from scipy.ndimage import filters

from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    MetadataItemType,
    LabelsType, LabelType,
    to_xml, AlternativeImageType
)
from ocrd import Processor
from ocrd_utils import (
    getLogger,
    make_file_id,
    assert_file_grp_cardinality,
    coordinates_of_segment,
    coordinates_for_segment,
    bbox_from_polygon,
    points_from_polygon,
    MIMETYPE_PAGE
)

from .. import get_ocrd_tool
from .ocrolib import midrange
from .common import (
    pil2array,
    # binarize,
    check_region,
    compute_segmentation
    #borderclean_bin
)


TOOL = 'ocrd-cis-ocropy-resegment'
LOG = getLogger('processor.OcropyResegment')

def resegment(line_polygon, region_labels, region_bin, line_id,
              extend_margins=3,
              threshold_relative=0.8, threshold_absolute=50):
    """Reduce line polygon in a labelled region to the largest intersection.

    Given a Numpy array ``line_polygon`` of relative coordinates
    in a region given by a Numpy array ``region_labels`` of numbered
    segments and a Numpy array ``region_bin`` of foreground pixels,
    find the label of the largest segment that intersects the polygon.
    If the number of foreground pixels within that segment is larger
    than ``threshold_absolute`` and if the share of foreground pixels
    within the whole polygon is larger than ``threshold_relative``,
    then compute the contour of that intersection and return it
    as a new polygon. Otherwise, return None.

    If ``extend_margins`` is larger than zero, then extend ``line_polygon``
    by that amount of pixels horizontally and vertically before.
    """
    # height, width = region_labels.shape
    # mask from line polygon:
    line_mask = np.zeros_like(region_labels)
    line_mask[draw.polygon(line_polygon[:,1], line_polygon[:,0], line_mask.shape)] = 1
    line_mask[draw.polygon_perimeter(line_polygon[:,1], line_polygon[:,0], line_mask.shape)] = 1
    #DSAVE('line %s mask' % line_id, line_mask + 0.5 * region_bin)
    # pad line polygon (extend the mask):
    line_mask = filters.maximum_filter(line_mask, 1 + 2 * extend_margins)
    # intersect with region labels
    line_labels = region_labels * line_mask
    if not np.count_nonzero(line_labels):
        LOG.warning('Label mask is empty for line "%s"', line_id)
        return None
    # find the mask of the largest label (in the foreground):
    total_count = np.sum(region_bin * line_mask)
    line_labels_fg = region_bin * line_labels
    if not np.count_nonzero(line_labels_fg):
        LOG.warning('No foreground pixels within line mask for line "%s"', line_id)
        return None
    label_counts = np.bincount(line_labels_fg.flat)
    max_label = np.argmax(label_counts[1:]) + 1
    max_count = label_counts[max_label]
    if (max_count < threshold_absolute and
        max_count / total_count < threshold_relative):
        LOG.info('Largest label (%d) is too small (%d/%d) in line "%s"',
                 max_label, max_count, total_count, line_id)
        return None
    LOG.debug('Black pixels before/after resegment of line "%s" (nlabels=%d): %d/%d',
              line_id, len(label_counts.nonzero()[0]), total_count, max_count)
    line_mask = np.array(line_labels == max_label, np.uint8)
    #DSAVE('line %s mask tight' % line_id, line_mask + 0.5 * region_bin)
    # find outer contour (parts):
    contours, _ = cv2.findContours(line_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    # determine largest part by area:
    contour_areas = [cv2.contourArea(contour) for contour in contours]
    max_contour = np.argmax(contour_areas)
    max_area = contour_areas[max_contour]
    total_area = cv2.contourArea(np.expand_dims(line_polygon, 1))
    if max_area / total_area < 0.5 * threshold_relative:
        # using a different, more conservative threshold here:
        # avoid being overly strict with cropping background,
        # just ensure the contours are not a split of the mask
        LOG.warning('Largest label (%d) largest contour (%d) is small (%d/%d) in line "%s"',
                    max_label, max_contour, max_area, total_area, line_id)
    contour = contours[max_contour]
    # simplify shape:
    # can produce invalid (self-intersecting) polygons:
    #polygon = cv2.approxPolyDP(contour, 2, False)[:, 0, ::] # already ordered x,y
    polygon = contour[:, 0, ::] # already ordered x,y
    polygon = Polygon(polygon).simplify(2).exterior.coords[:-1] # keep open
    if len(polygon) < 4:
        LOG.warning('found no contour of >=4 points for line "%s"', line_id)
        return None
    return polygon

class OcropyResegment(Processor):

    def __init__(self, *args, **kwargs):
        self.ocrd_tool = get_ocrd_tool()
        kwargs['ocrd_tool'] = self.ocrd_tool['tools'][TOOL]
        kwargs['version'] = self.ocrd_tool['version']
        super(OcropyResegment, self).__init__(*args, **kwargs)

    def process(self):
        """Resegment lines of the workspace.

        Open and deserialise PAGE input files and their respective images,
        then iterate over the element hierarchy down to the line level.

        Next, get each region image according to the layout annotation (from
        the alternative image of the region, or by cropping via coordinates
        into the higher-level image), and compute a new line segmentation
        from that (as a label mask).

        Then for each line within the region, find the label with the largest
        foreground area in the binarized image within the annotated polygon
        (or rectangle) of the line. Unless its relative area is too small,
        or its center is far off, convert that label's mask into a polygon
        outline, intersect with the old polygon, and find the contour of that
        segment. Annotate the result as new coordinates of the line.

        Add the new image file to the workspace along with the output fileGrp,
        and using a file ID with suffix ``.IMG-RESEG`` along with further
        identification of the input element.

        Produce a new output file by serialising the resulting hierarchy.
        """
        # This makes best sense for bad/coarse line segmentation, like current GT
        # or as postprocessing for bbox-only steps.
        # Most notably, it can convert rectangles to polygons (polygonalization).
        # It depends on a decent line segmentation from ocropy though. So it
        # _should_ ideally be run after deskewing (on the page or region level),
        # _must_ be run after binarization (on page or region level). Also, the
        # method's accuracy crucially depends on a good estimate of the images'
        # pixel density (at least if source input is not 300 DPI).
        threshold = self.parameter['min_fraction']
        margin = self.parameter['extend_margins']
        assert_file_grp_cardinality(self.input_file_grp, 1)
        assert_file_grp_cardinality(self.output_file_grp, 1)

        for (n, input_file) in enumerate(self.input_files):
            LOG.info("INPUT FILE %i / %s", n, input_file.pageId or input_file.ID)
            file_id = make_file_id(input_file, self.output_file_grp)

            pcgts = page_from_file(self.workspace.download_file(input_file))
            page_id = pcgts.pcGtsId or input_file.pageId or input_file.ID # (PageType has no id)
            page = pcgts.get_Page()
            
            # add metadata about this operation and its runtime parameters:
            metadata = pcgts.get_Metadata() # ensured by from_file()
            metadata.add_MetadataItem(
                MetadataItemType(type_="processingStep",
                                 name=self.ocrd_tool['steps'][0],
                                 value=TOOL,
                                 Labels=[LabelsType(
                                     externalModel="ocrd-tool",
                                     externalId="parameters",
                                     Label=[LabelType(type_=name,
                                                      value=self.parameter[name])
                                            for name in self.parameter.keys()])]))
            
            page_image, page_xywh, page_image_info = self.workspace.image_from_page(
                page, page_id, feature_selector='binarized')
            if self.parameter['dpi'] > 0:
                zoom = 300.0/self.parameter['dpi']
            elif page_image_info.resolution != 1:
                dpi = page_image_info.resolution
                if page_image_info.resolutionUnit == 'cm':
                    dpi *= 2.54
                LOG.info('Page "%s" uses %f DPI', page_id, dpi)
                zoom = 300.0/dpi
            else:
                zoom = 1

            regions = page.get_TextRegion()
            if not regions:
                LOG.warning('Page "%s" contains no text regions', page_id)
            for region in regions:
                lines = region.get_TextLine()
                if not lines:
                    LOG.warning('Page "%s" region "%s" contains no text lines', page_id, region.id)
                    continue
                if len(lines) == 1:
                    LOG.warning('Page "%s" region "%s" contains only one line', page_id, region.id)
                    continue
                region_image, region_xywh = self.workspace.image_from_segment(
                    region, page_image, page_xywh, feature_selector='binarized')
                region_array = pil2array(region_image)
                #region_array, _ = common.binarize(region_array, maxskew=0) # just in case still raw
                region_bin = np.array(region_array <= midrange(region_array), np.bool)
                report = check_region(region_bin, zoom)
                try:
                    if report:
                        raise Exception(report)
                    region_labels, _, _, _, _, _ = compute_segmentation(region_bin, zoom=zoom)
                except Exception as err:
                    LOG.warning('Cannot line-segment page "%s" region "%s": %s',
                                page_id, region.id, err)
                    # fallback option 1: borderclean
                    # label margins vs interior, but with the interior
                    # extended into the margin by its connected components
                    # to remove noise from neighbouring regions:
                    #region_labels = borderclean_bin(region_bin, margin=round(4/zoom)) + 1
                    # too dangerous, because we risk losing dots from i or punctuation;
                    # fallback option2: only extend_margins
                    # instead, just provide a uniform label, so at least we get
                    # to extend the polygon margins:
                    #region_labels = np.ones_like(region_bin)
                    # fallback option3: keep unchanged
                    continue
                for line in lines:
                    if line.get_AlternativeImage():
                        # get cropped line image:
                        line_image, line_xywh = self.workspace.image_from_segment(
                            line, region_image, region_xywh, feature_selector='binarized')
                        LOG.debug("Using AlternativeImage (%s) for line '%s'",
                                  line_xywh['features'], line.id)
                        # crop region arrays accordingly:
                        line_polygon = coordinates_of_segment(line, region_image, region_xywh)
                        line_bbox = bbox_from_polygon(line_polygon)
                        line_labels = region_labels[line_bbox[1]:line_bbox[3],
                                                    line_bbox[0]:line_bbox[2]]
                        line_bin = region_bin[line_bbox[1]:line_bbox[3],
                                              line_bbox[0]:line_bbox[2]]
                        # get polygon in relative (line) coordinates:
                        line_polygon = coordinates_of_segment(line, line_image, line_xywh)
                        line_polygon = resegment(line_polygon, line_labels, line_bin, line.id,
                                                 extend_margins=margin, threshold_relative=threshold)
                        if line_polygon is None:
                            continue # not good enough – keep
                        # convert back to absolute (page) coordinates:
                        line_polygon = coordinates_for_segment(line_polygon, line_image, line_xywh)
                    else:
                        # get polygon in relative (region) coordinates:
                        line_polygon = coordinates_of_segment(line, region_image, region_xywh)
                        line_polygon = resegment(line_polygon, region_labels, region_bin, line.id,
                                                 extend_margins=margin, threshold_relative=threshold)
                        if line_polygon is None:
                            continue # not good enough – keep
                        # convert back to absolute (page) coordinates:
                        line_polygon = coordinates_for_segment(line_polygon, region_image, region_xywh)
                    # annotate result:
                    line.get_Coords().points = points_from_polygon(line_polygon)
                    # create new image:
                    line_image, line_xywh = self.workspace.image_from_segment(
                        line, region_image, region_xywh, feature_selector='binarized')
                    # update METS (add the image file):
                    file_path = self.workspace.save_image_file(
                        line_image,
                        file_id=file_id + '_' + region.id + '_' + line.id + '.IMG-RESEG',
                        page_id=page_id,
                        file_grp=self.output_file_grp)
                    # update PAGE (reference the image file):
                    line.add_AlternativeImage(AlternativeImageType(
                        filename=file_path,
                        comments=region_xywh['features']))

            # update METS (add the PAGE file):
            file_path = os.path.join(self.output_file_grp, file_id + '.xml')
            pcgts.set_pcGtsId(file_id)
            out = self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                local_filename=file_path,
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts))
            LOG.info('created file ID: %s, file_grp: %s, path: %s',
                     file_id, self.output_file_grp, out.local_filename)
