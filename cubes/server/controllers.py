from werkzeug.wrappers import Response
from werkzeug.utils import redirect
from werkzeug.exceptions import NotFound
import sqlalchemy
import decimal
import os.path
import logging
import urllib
import datetime

version = "0.1"
api_version = "0"

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'templates')

import cubes
import json

class FixingEncoder(json.JSONEncoder):
    def default(self, o):
        if type(o) == decimal.Decimal:
            return float(o)
        if type(o) == datetime.date or type(o) == datetime.datetime:
            return o.isoformat()
        else:
            return json.JSONEncoder.default(self, o)

class ApplicationController(object):
    def __init__(self, config):
        self.model = cubes.load_model(config["model"])
        self.cube_name = config["cube"]
        self.cube = self.model.cube(self.cube_name)

        if "view" in config:
            self.view_name = config["view"]
        else:
            self.view_name = self.cube_name

        if "schema" in config:
            self.schema = config["schema"]
        else:
            self.schema = None

        self.dburl = config["connection"]

        self.params = None
        self.query = None
        self.browser = None
        self.locale = None

        
        ppflag = self.request.args.get("prettyprint")
        if ppflag:
            ppflag = ppflag.lower()
            if ppflag in ["true", "yes", "1"]:
                self.prettyprint = True
            else:
                self.prettyprint = False
        else:
            self.prettyprint = False
        
    def index(self):
        handle = open(os.path.join(TEMPLATE_PATH, "index.html"))
        template = handle.read()
        handle.close()
        
        context = {"version": version, "api_version": api_version}
        
        context["model"] = self.model.name
        array = []
        for cube in self.model.cubes.values():
            array.append(cube.name)
            
        if array:
            context["cubes"] = ", ".join(array)
        else:
            context["cubes"] = "<none>"
        
        doc = template.format(**context)
        
        return Response(doc, mimetype = 'text/html')

    def load_model(self):
        pass

    def __set_model(self, model):
        self._model = model
        if self.locale:
            self._localized_model = cubes.localize_model(model)
        else:
            self._localized_model = model

    def __get_model(self):
        return self._localized_model
    
    def version(self):
        response = {
            "server_version": version,
            "api_version": api_version
        }

        return self.json_response(response)

    def json_response(self, obj):
        if self.prettyprint:
            indent = 4
        else:
            indent = None
        
        encoder = FixingEncoder(indent = indent)
        reply = encoder.iterencode(obj)

        return Response(reply, mimetype='application/json')
        
    def initialize(self):
        pass
        
    def finalize(self):
        pass
        
    def error(self, message = None, exception = None, status = None):
        if not message:
            message = "An unknown error occured"
            
        error = {}
        error["message"] = message
        if exception:
            error["reason"] = str(exception)

        string = json.dumps({"error": error},indent = 4)
        
        if not status:
            status = 500
        
        return Response(string, mimetype='application/json', status = status)
        
class ModelController(ApplicationController):

    def show(self):
        return self.json_response(self.model.to_dict(with_mappings = False))

    def dimension(self):
        dim_name = self.params["name"]

        dim = self.model.dimension(dim_name)
        return self.json_response(dim.to_dict())

    def _cube_dict(self, cube):
        d = cube.to_dict(expand_dimensions = True, 
                         with_mappings = False,
                         full_attribute_names = True
                         )

        return d

    def get_default_cube(self):
        return self.json_response(self._cube_dict(self.cube))

    def get_cube(self):
        cube_name = self.params["name"]

        cube = self.model.cube(cube_name)
        return self.json_response(self._cube_dict(cube))
        
    def dimension_levels(self):
        dim_name = self.params["name"]
        dim = self.model.dimension(dim_name)
        levels = [l.to_dict() for l in dim.default_hierarchy.levels]

        string = json.dumps(levels)

        return Response(string)

    def dimension_level_names(self):
        dim_name = self.params["name"]
        dim = self.model.dimension(dim_name)

        return self.json_response(dim.default_hierarchy.level_names)

class AggregationController(ApplicationController):
    def initialize(self):

        self.engine = sqlalchemy.create_engine(self.dburl)
        
        self.connection = self.engine.connect()

        self.browser = cubes.backends.SQLBrowser(self.cube,
                                                    self.connection, 
                                                    self.view_name,
                                                    self.schema)


        if "page" in self.request.args:
            self.page = int(self.request.args.get("page"))
        else:
            self.page = None
        if "pagesize" in self.request.args:
            self.page_size = int(self.request.args.get("pagesize"))
        else:
            self.page_size = None
            
        # Collect orderings:
        # order is specified as order=<field>[:<direction>]
        # examples:
        #
        #     order=date.year     # order by year, unspecified direction
        #     order=date.year:asc # order by year ascending
        #
        
        self.order = []
        for order in self.request.args.getlist("order"):
            split = order.split(":")
            if len(split) == 1:
                self.order.append( (order, None) )
            else:
                self.order.append( (split[0], split[1]) )
        
    def finalize(self):
        if self.browser:
            del self.browser

        if self.connection:
            self.connection.close()
            self.engine.dispose()
            del self.connection
            del self.engine
    
    def prepare_cuboid(self):
        cut_string = self.request.args.get("cut")

        if cut_string:
            cuts = cubes.cuts_from_string(cut_string)
        else:
            cuts = []

        self.cuboid = cubes.Cuboid(self.browser, cuts)
        
    def aggregate(self):
        self.prepare_cuboid()

        drilldown = self.request.args.getlist("drilldown")

        try:
            result = self.cuboid.aggregate(drilldown = drilldown, 
                                            page = self.page, 
                                            page_size = self.page_size,
                                            order = self.order)
        except Exception, e:
            return self.error("Aggregation failed", e)

        # return Response(result.as_json())
        return self.json_response(result.as_dict())

    def facts(self):
        self.prepare_cuboid()

        try:
            result = self.cuboid.facts(page = self.page, page_size = self.page_size)
        except Exception, e:
            return self.error("Fetching facts failed", e)

        return self.json_response(result)

    def fact(self):
        fact_id = self.params["id"]

        try:
            fact = self.browser.fact(fact_id)
        except Exception, e:
            return self.error("Fetching single fact failed", e)

        if fact:
            return self.json_response(fact)
        else:
            return self.error("No fact with id=%s" % fact_id, status = 404)
        
    def values(self):
        self.prepare_cuboid()

        dim_name = self.params["dimension"]
        depth_string = self.request.args.get("depth")
        if depth_string:
            try:
                depth = int(self.request.args.get("depth"))
            except:
                return self.error("depth should be an integer")
        else:
            depth = None
        
        try:
            dimension = self.cube.dimension(dim_name)
        except:
            return self.error("No dimension '%s'" % dim_name, status = 404)

        try:
            values = self.cuboid.values(dimension, depth = depth, page = self.page, page_size = self.page_size)
        except Exception, e:
            return self.error("Getting values for dimension %s failed" % dim_name, e)

        result = {
            "dimension": dimension.name,
            "depth": depth,
            "data": values
        }
        
        return self.json_response(result)
        