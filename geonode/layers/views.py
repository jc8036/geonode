# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2012 OpenPlans
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

import os
import logging
import shutil

from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response, render # render added by ict4eo for ncWMS
from django.conf import settings
from django.template import RequestContext
from django.utils.translation import ugettext as _
from django.utils import simplejson as json
from django.utils.html import escape
from django.template.defaultfilters import slugify
from django.forms.models import inlineformset_factory
from django.db.models import F

from geonode.services.models import Service
from geonode.layers.forms import LayerForm, LayerUploadForm, NewLayerUploadForm, LayerAttributeForm
from geonode.base.forms import CategoryForm
from geonode.layers.models import Layer, Attribute
from geonode.base.enumerations import CHARSETS
from geonode.base.models import TopicCategory

from geonode.utils import default_map_config, llbbox_to_mercator
from geonode.utils import GXPLayer
from geonode.utils import GXPMap
from geonode.layers.utils import file_upload
from geonode.utils import resolve_object
from geonode.people.forms import ProfileForm, PocForm
from geonode.security.views import _perms_info_json
from geonode.documents.models import get_related_documents
from geoserver.resource import FeatureType
from geonode.contrib.groups.models import Group

# imports for sSOS
if 'geonode.geoserver' in settings.INSTALLED_APPS:
    from geonode.geoserver.helpers import ogc_server_settings
    from geonode.geoserver.ows import sos_swe_data_list, sos_observation_xml

# imports for ncWMS
from owslib.wms import WebMapService
from geonode.layers.openDap import _opendap_links, opendap_links 

logger = logging.getLogger("geonode.layers.views")

DEFAULT_SEARCH_BATCH_SIZE = 10
MAX_SEARCH_BATCH_SIZE = 25
GENERIC_UPLOAD_ERROR = _("There was an error while attempting to upload your data. \
Please try again, or contact and administrator if the problem continues.")

_PERMISSION_MSG_DELETE = _("You are not permitted to delete this layer")
_PERMISSION_MSG_GENERIC = _('You do not have permissions for this layer.')
_PERMISSION_MSG_MODIFY = _("You are not permitted to modify this layer")
_PERMISSION_MSG_METADATA = _(
    "You are not permitted to modify this layer's metadata")
_PERMISSION_MSG_VIEW = _("You are not permitted to view this layer")


def _resolve_layer(request, typename, permission='base.view_resourcebase',
                   msg=_PERMISSION_MSG_GENERIC, **kwargs):
    """
    Resolve the layer by the provided typename (which may include service name) and check the optional permission.
    """
    service_typename = typename.split(":", 1)
    service = Service.objects.filter(name=service_typename[0])

    if service.count() > 0 and service[0].method != "C":
        return resolve_object(request,
                              Layer,
                              {'service': service[0],
                               'typename': service_typename[1]},
                              permission=permission,
                              permission_msg=msg,
                              **kwargs)
    else:
        return resolve_object(request,
                              Layer,
                              {'typename': typename,
                               'service': None},
                              permission=permission,
                              permission_msg=msg,
                              **kwargs)


# Basic Layer Views #


@login_required
def layer_upload(request, template='upload/layer_upload.html'):
    if request.method == 'GET':
        ctx = {
            'charsets': CHARSETS
        }
        return render_to_response(template,
                                  RequestContext(request, ctx))
    elif request.method == 'POST':
        form = NewLayerUploadForm(request.POST, request.FILES)
        tempdir = None
        errormsgs = []
        out = {'success': False}

        if form.is_valid():
            title = form.cleaned_data["layer_title"]

            # Replace dots in filename - GeoServer REST API upload bug
            # and avoid any other invalid characters.
            # Use the title if possible, otherwise default to the filename
            if title is not None and len(title) > 0:
                name_base = title
            else:
                name_base, __ = os.path.splitext(
                    form.cleaned_data["base_file"].name)

            name = slugify(name_base.replace(".", "_"))

            try:
                # Moved this inside the try/except block because it can raise
                # exceptions when unicode characters are present.
                # This should be followed up in upstream Django.
                tempdir, base_file = form.write_files()
                saved_layer = file_upload(
                    base_file,
                    name=name,
                    user=request.user,
                    overwrite=False,
                    charset=form.cleaned_data["charset"],
                    abstract=form.cleaned_data["abstract"],
                    title=form.cleaned_data["layer_title"],
                )

            except Exception as e:
                logger.exception(e)
                out['success'] = False
                out['errors'] = str(e)
            else:
                out['success'] = True
                out['url'] = reverse(
                    'layer_detail', args=[
                        saved_layer.service_typename])

                permissions = form.cleaned_data["permissions"]
                if permissions is not None and len(permissions.keys()) > 0:
                    saved_layer.set_permissions(permissions)

            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            for e in form.errors.values():
                errormsgs.extend([escape(v) for v in e])

            out['errors'] = form.errors
            out['errormsgs'] = errormsgs

        if out['success']:
            status_code = 200
        else:
            status_code = 500
        return HttpResponse(
            json.dumps(out),
            mimetype='application/json',
            status=status_code)


def layer_detail(request, layername, template='layers/layer_detail.html'):

    keys = [lkw.name for lkw in layer.keywords.all()]
    if 'sos' in keys or 'SOS' in keys:
        template = 'layers/layer_detail_SOS.html'
    #elif 'netcdf' in keys or 'NetCDF' in keys: 
    #   template = 'layers/layer_detail_NetCDF.html'  
    layer = _resolve_layer(
        request,
        layername,
        'base.view_resourcebase',
        _PERMISSION_MSG_VIEW)
    layer_bbox = layer.bbox
    # assert False, str(layer_bbox)
    bbox = list(layer_bbox[0:4])
    config = layer.attribute_config()

    # Add required parameters for GXP lazy-loading
    config["srs"] = layer.srid
    config["title"] = layer.title
    config["bbox"] = [float(coord) for coord in bbox] if layer.srid == "EPSG:4326" else llbbox_to_mercator(
        [float(coord) for coord in bbox])

    if layer.storeType == "remoteStore":
        service = layer.service
        source_params = {
            "ptype": service.ptype,
            "remote": True,
            "url": service.base_url,
            "name": service.name}
        maplayer = GXPLayer(
            name=layer.typename,
            ows_url=layer.ows_url,
            layer_params=json.dumps(config),
            source_params=json.dumps(source_params))
    else:
        maplayer = GXPLayer(
            name=layer.typename,
            ows_url=layer.ows_url,
            layer_params=json.dumps(config))

    # Update count for popularity ranking.
    Layer.objects.filter(
        id=layer.id).update(popular_count=F('popular_count') + 1)

    # center/zoom don't matter; the viewer will center on the layer bounds
    map_obj = GXPMap(projection="EPSG:900913")
    NON_WMS_BASE_LAYERS = [
        la for la in default_map_config()[1] if la.ows_url is None]

    metadata = layer.link_set.metadata().filter(
        name__in=settings.DOWNLOAD_FORMATS_METADATA)

    context_dict = {
        "resource": layer,
        "permissions_json": _perms_info_json(layer),
        "documents": get_related_documents(layer),
        "metadata": metadata,
    }

    context_dict["viewer"] = json.dumps(
        map_obj.viewer_json(request.user, * (NON_WMS_BASE_LAYERS + [maplayer])))
    context_dict["preview"] = getattr(
        settings,
        'LAYER_PREVIEW_LIBRARY',
        'leaflet')

    if layer.storeType == 'dataStore':
        links = layer.link_set.download().filter(
            name__in=settings.DOWNLOAD_FORMATS_VECTOR)
    else:
        links = layer.link_set.download().filter(
            name__in=settings.DOWNLOAD_FORMATS_RASTER)

    context_dict["links"] = links

    return render_to_response(template, RequestContext(request, context_dict))


@login_required
def layer_metadata(request, layername, template='layers/layer_metadata.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.change_resourcebase',
        _PERMISSION_MSG_METADATA)
    layer_attribute_set = inlineformset_factory(
        Layer,
        Attribute,
        extra=0,
        form=LayerAttributeForm,
    )
    topic_category = layer.category

    poc = layer.poc
    metadata_author = layer.metadata_author

    if request.method == "POST":
        layer_form = LayerForm(request.POST, instance=layer, prefix="resource")
        attribute_form = layer_attribute_set(
            request.POST,
            instance=layer,
            prefix="layer_attribute_set",
            queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(
            request.POST,
            prefix="category_choice_field",
            initial=int(
                request.POST["category_choice_field"]) if "category_choice_field" in request.POST else None)
    else:
        layer_form = LayerForm(instance=layer, prefix="resource")
        attribute_form = layer_attribute_set(
            instance=layer,
            prefix="layer_attribute_set",
            queryset=Attribute.objects.order_by('display_order'))
        category_form = CategoryForm(
            prefix="category_choice_field",
            initial=topic_category.id if topic_category else None)

    if request.method == "POST" and layer_form.is_valid(
    ) and attribute_form.is_valid() and category_form.is_valid():
        new_poc = layer_form.cleaned_data['poc']
        new_author = layer_form.cleaned_data['metadata_author']
        new_keywords = layer_form.cleaned_data['keywords']

        if new_poc is None:
            if poc is None:
                poc_form = ProfileForm(
                    request.POST,
                    prefix="poc",
                    instance=poc)
            else:
                poc_form = ProfileForm(request.POST, prefix="poc")
            if poc_form.has_changed and poc_form.is_valid():
                new_poc = poc_form.save()

        if new_author is None:
            if metadata_author is None:
                author_form = ProfileForm(request.POST, prefix="author",
                                          instance=metadata_author)
            else:
                author_form = ProfileForm(request.POST, prefix="author")
            if author_form.has_changed and author_form.is_valid():
                new_author = author_form.save()

        new_category = TopicCategory.objects.get(
            id=category_form.cleaned_data['category_choice_field'])

        for form in attribute_form.cleaned_data:
            la = Attribute.objects.get(id=int(form['id'].id))
            la.description = form["description"]
            la.attribute_label = form["attribute_label"]
            la.visible = form["visible"]
            la.display_order = form["display_order"]
            la.save()

        if new_poc is not None and new_author is not None:
            the_layer = layer_form.save()
            the_layer.poc = new_poc
            the_layer.metadata_author = new_author
            the_layer.keywords.clear()
            the_layer.keywords.add(*new_keywords)
            the_layer.category = new_category
            the_layer.save()
            return HttpResponseRedirect(
                reverse(
                    'layer_detail',
                    args=(
                        layer.service_typename,
                    )))

    if poc is None:
        poc_form = ProfileForm(instance=poc, prefix="poc")
    else:
        layer_form.fields['poc'].initial = poc.id
        poc_form = ProfileForm(prefix="poc")
        poc_form.hidden = True

    if metadata_author is None:
        author_form = ProfileForm(instance=metadata_author, prefix="author")
    else:
        layer_form.fields['metadata_author'].initial = metadata_author.id
        author_form = ProfileForm(prefix="author")
        author_form.hidden = True

    return render_to_response(template, RequestContext(request, {
        "layer": layer,
        "layer_form": layer_form,
        "poc_form": poc_form,
        "author_form": author_form,
        "attribute_form": attribute_form,
        "category_form": category_form,
    }))


@login_required
def layer_change_poc(request, ids, template='layers/layer_change_poc.html'):
    layers = Layer.objects.filter(id__in=ids.split('_'))
    if request.method == 'POST':
        form = PocForm(request.POST)
        if form.is_valid():
            for layer in layers:
                layer.poc = form.cleaned_data['contact']
                layer.save()
            # Process the data in form.cleaned_data
            # ...
            # Redirect after POST
            return HttpResponseRedirect('/admin/maps/layer')
    else:
        form = PocForm()  # An unbound form
    return render_to_response(
        template, RequestContext(
            request, {
                'layers': layers, 'form': form}))


@login_required
def layer_replace(request, layername, template='layers/layer_replace.html'):
    layer = _resolve_layer(
        request,
        layername,
        'base.change_resourcebase',
        _PERMISSION_MSG_MODIFY)

    if request.method == 'GET':
        ctx = {
            'charsets': CHARSETS,
            'layer': layer,
            'is_featuretype': layer.is_vector()
        }
        return render_to_response(template,
                                  RequestContext(request, ctx))
    elif request.method == 'POST':

        form = LayerUploadForm(request.POST, request.FILES)
        tempdir = None
        out = {}

        if form.is_valid():
            try:
                tempdir, base_file = form.write_files()
                saved_layer = file_upload(
                    base_file,
                    name=layer.name,
                    user=request.user,
                    overwrite=True,
                    charset=form.cleaned_data["charset"],
                )
            except Exception as e:
                out['success'] = False
                out['errors'] = str(e)
            else:
                out['success'] = True
                out['url'] = reverse(
                    'layer_detail', args=[
                        saved_layer.service_typename])
            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
        else:
            errormsgs = []
            for e in form.errors.values():
                errormsgs.append([escape(v) for v in e])

            out['errors'] = form.errors
            out['errormsgs'] = errormsgs

        if out['success']:
            status_code = 200
        else:
            status_code = 500
        return HttpResponse(
            json.dumps(out),
            mimetype='application/json',
            status=status_code)


@login_required
def layer_remove(request, layername, template='layers/layer_remove.html'):
    try:
        layer = _resolve_layer(request, layername, 'base.delete_resourcebase',
                               _PERMISSION_MSG_DELETE)

        if (request.method == 'GET'):
            return render_to_response(template, RequestContext(request, {
                "layer": layer
            }))
        if (request.method == 'POST'):
            layer.delete()
            return HttpResponseRedirect(reverse("layer_browse"))
        else:
            return HttpResponse("Not allowed", status=403)
    except PermissionDenied:
        return HttpResponse(
            'You are not allowed to delete this layer',
            mimetype="text/plain",
            status=401
        )


def resolve_user(request):
    user = None
    geoserver = False
    superuser = False
    if 'HTTP_AUTHORIZATION' in request.META:
        username, password = _get_basic_auth_info(request)
        acl_user = authenticate(username=username, password=password)
        if acl_user:
            user = acl_user.username
            superuser = acl_user.is_superuser
        elif _get_basic_auth_info(request) == ogc_server_settings.credentials:
            geoserver = True
            superuser = True
        else:
            return HttpResponse(_("Bad HTTP Authorization Credentials."),
                                status=401,
                                mimetype="text/plain")
    if not any([user, geoserver, superuser]) and not request.user.is_anonymous():
        user = request.user.username
        superuser = request.user.is_superuser
    resp = {
        'user' : user,
        'geoserver' : geoserver,
        'superuser' : superuser,
    }
    if request.user.is_authenticated():
        resp['fullname'] = request.user.profile.name
        resp['email'] = request.user.profile.email
    return HttpResponse(json.dumps(resp))


def layer_acls(request):
    """
    returns json-encoded lists of layer identifiers that
    represent the sets of read-write and read-only layers
    for the currently authenticated user.
    """

    # the layer_acls view supports basic auth, and a special
    # user which represents the geoserver administrator that
    # is not present in django.
    acl_user = request.user
    if 'HTTP_AUTHORIZATION' in request.META:
        try:
            username, password = _get_basic_auth_info(request)
            acl_user = authenticate(username=username, password=password)

            # Nope, is it the special geoserver user?
            if (acl_user is None and
                username == ogc_server_settings.USER and
                password == ogc_server_settings.PASSWORD):
                # great, tell geoserver it's an admin.
                result = {
                   'rw': [],
                   'ro': [],
                   'name': username,
                   'is_superuser':  True,
                   'is_anonymous': False
                }
                return HttpResponse(json.dumps(result), mimetype="application/json")
        except Exception:
            pass

        if acl_user is None:
            return HttpResponse(_("Bad HTTP Authorization Credentials."),
                                status=401,
                                mimetype="text/plain")
    all_readable = set()
    all_writable = set()
    for bck in get_auth_backends():
        if hasattr(bck, 'objects_with_perm'):
            all_readable.update(bck.objects_with_perm(acl_user,
                                                      'layers.view_layer',
                                                      Layer))
            all_writable.update(bck.objects_with_perm(acl_user,
                                                      'layers.change_layer',
                                                      Layer))
    read_only = [x for x in all_readable if x not in all_writable]
    read_write = [x for x in all_writable if x in all_readable]

    read_only = [x[0] for x in Layer.objects.filter(id__in=read_only).values_list('typename').all()]
    read_write = [x[0] for x in Layer.objects.filter(id__in=read_write).values_list('typename').all()]

    result = {
        'rw': read_write,
        'ro': read_only,
        'name': acl_user.username,
        'is_superuser':  acl_user.is_superuser,
        'is_anonymous': acl_user.is_anonymous(),
    }
    if acl_user.is_authenticated():
        result['fullname'] = acl_user.profile.name
        result['email'] = acl_user.profile.email

    return HttpResponse(json.dumps(result), mimetype="application/json")

def feature_edit_check(request, layername):
    """
    If the layer is not a raster and the user has edit permission, return a status of 200 (OK).
    Otherwise, return a status of 401 (unauthorized).
    """
    layer = get_object_or_404(Layer, typename=layername)
    feature_edit = getattr(settings, "GEOGIT_DATASTORE", None) or ogc_server_settings.DATASTORE 
    if request.user.has_perm('layers.change_layer', obj=layer) and layer.storeType == 'dataStore' and feature_edit:
        return HttpResponse(json.dumps({'authorized': True}), mimetype="application/json")
    else:
        return HttpResponse(json.dumps({'authorized': False}), mimetype="application/json")

############################### META DATA #####################################


def get_metadata(request, layername):
    """Return metadata for a layer specified by name.
    
    Access to the layer keywords is needed to determine the additional 
    characteristics of a layer. 

    Access to the layer's supplemental information at this level must be 
    parsed for appropriate function.
    """
    layer = _resolve_layer(request, layername, 'layers.view_layer', _PERMISSION_MSG_VIEW)
    if 'feature' in request.GET:
        feature = request.GET['feature']
    else:
        feature = None
    keys = [lkw.name for lkw in layer.keywords.all()]
    sup_inf_str = str(layer.supplemental_information) 
    if "sos" in keys or "SOS" in keys:
        return layer_sos_csv(feature, sup_inf_str, time=None)
    #elif "netcdf" in keys or "NetCDF" in keys:
        #return netcdf_layer_csv(request, layername, time=None)
    else:
        return None

################################## SOS DATA ##################################


def layer_sos_csv(feature, supplementary_info, time=None):
    """Return SOS data in CSV format for a layer that specifies a valid SOS URL.
    
    Parameters
    ----------
    feature : string
        the ID of a feature from the WFS; this is used as a link to a
        corresponding "feature_of_interest" in the SOS
    supplementary_info : dictionary
        a set of parameters used to access a SOS.  For example:
            {"sos_url": "http://sos.server.com:8080/sos", 
             "observedProperties": ["urn:ogc:def:phenomenon:OGC:1.0.30:temperature"], 
             "offerings": ["WEATHER"]}
    time : string
        Optional.   Time should conform to ISO format: YYYY-MM-DDTHH:mm:ss+-HH
        Instance is given as one time value. Periods of time (start and end) are
        separated by "/". Example: 2009-06-26T10:00:00+01/2009-06-26T11:00:00+01
    """
    import csv
    sup_info = eval(supplementary_info)
    offerings = sup_info.get('offerings')
    url = sup_info.get('sos_url')
    observedProperties = sup_info.get('observedProperties')
    time = time
    XML = sos_observation_xml(
        url, offerings=offerings, observedProperties=observedProperties, 
        allProperties=False, feature=feature, eventTime=time)
    lists = sos_swe_data_list(XML)
    sos_data = HttpResponse(mimetype='text/csv')
    sos_data['Content-Disposition'] = 'attachment;filename=sos.csv'
    writer = csv.writer(sos_data)
    # headers are included by default in lists, can set show_headers to false
    #   in the sos_swe_data_list() in ows.py
    writer.writerows(lists)
    return sos_data
    # TODO set the response to return JSON including format, data and style info
    # service_result =  { format: ...,
    #                    'data': sos_data,
    #                     style: ...}
    # return HttpResponse(json.dumps(service_result), mimetype="application/json")
        
################ NETCDF DATA  via ncWMS: WMST #################################


def layer_wmst(request, template='layers/layer_wmst.html'):
    return render(request, template)


def layer_wmst_search(request, template='layers/layer_wmst.html'):
    if 'wms_url' in request.GET and request.GET['wms_url'] and \
    'wms_version' in request.GET and request.GET['wms_version']: 
		url = request.GET['wms_url']
		version = request.GET['wms_version']
		wms = WebMapService(url, version)
		request.session['wms']= wms
		request.session['url']= url
		layers = list(wms.contents)
		wms_name = wms.identification.title
		return render_to_response(template, RequestContext(
		    request, {'layer_list': layers, 'name': wms_name}
		))
    else:
    	return render(request, template, {'error':True})
    	  	

def ncWms_detail(request, layerpart1, layerpart2, 
                 template='layers/ncWMS_layer_details.html'):
    """Handle Layer Details and Time Series Playback"""
    layer = layerpart1+ '/' + layerpart2
    wms1 = request.session['wms']
    w_url = request.session['url']
    wms_times = wms1[layer].timepositions
    times = [time.strip() for time in wms_times]
    wms_name = wms1.identification.title
    
    #for downloading layer
    download_links = ['full dataset', 'spatial subset', 'temporal subset', 'spatio-temporal subset' ]
    links = opendap_links()
    map_obj = GXPMap(projection="EPSG:900913")
    DEFAULT_BASE_LAYERS = default_map_config()[1]

    return render_to_response(template, RequestContext(request, {
        "w_name": wms_name,
        "w_url": w_url,
	    "layer": layer,
	    "w_times": json.dumps(times),
	    "links_" : download_links,
	    "links" : links,
        "viewer": json.dumps(map_obj.viewer_json(* (DEFAULT_BASE_LAYERS + []))),
    }))


def Wmst_detail(request, layername, template='layers/ncWMS_layer_details.html'):
    """Support (short-term solution) for GeoServer WMS-T."""
    layer = layername
    wms1 = request.session['wms']
    w_url = request.session['url']
    wms_times = wms1[layer].timepositions
    times = [time.strip() for time in wms_times]
    wms_name = wms1.identification.title
    
    #for downloading layer
    #download_links = ['full dataset', 'spatial subset', 'temporal subset', 'spatio-temporal subset' ]
    links = opendap_links()
    map_obj = GXPMap(projection="EPSG:900913")
    DEFAULT_BASE_LAYERS = default_map_config()[1]

    return render_to_response(template, RequestContext(request, {
        "w_name": wms_name,
        "w_url": w_url,
	    "layer": layer,
	    "w_times": json.dumps(times),
	    "links_" : download_links,
	    "links" : links,
        "viewer": json.dumps(map_obj.viewer_json(* (DEFAULT_BASE_LAYERS + []))),
    }))

=======
>>>>>>> 07331872739ddde19395d89c87719c45533c1d64
