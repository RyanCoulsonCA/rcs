"""
The starter module for RCS.  Currently it contains most of the functional code
for RCS and this should eventually end up in separate modules or packages.
"""
from __future__ import division, print_function, unicode_literals

import json, pycouchdb, requests, jsonschema, regparse, db, config, os, sys, logging, numbers

from functools import wraps
from logging.handlers import RotatingFileHandler
from flask import Flask, Blueprint, Response, current_app, got_request_exception
from flask.ext.restful import reqparse, request, abort, Api, Resource

# FIXME clean this up
app = Flask(__name__)
reload(sys)
sys.setdefaultencoding('utf8')
app.config.from_object(config)
if os.environ.get('RCS_CONFIG'):
    app.config.from_envvar('RCS_CONFIG')
handler = RotatingFileHandler( app.config['LOG_FILE'], maxBytes=app.config.get('LOG_ROTATE_BYTES',200000), backupCount=app.config.get('LOG_BACKUPS',5) )
handler.setLevel( app.config['LOG_LEVEL'] )
handler.setFormatter( logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s '
    '[in %(pathname)s:%(lineno)d]'
))

loggers = [app.logger, logging.getLogger('regparse.sigcheck')]
for l in loggers:
    l.addHandler( handler )


db.init_auth_db( app.config['DB_CONN'], app.config['AUTH_DB'] )
db.init_doc_db( app.config['DB_CONN'], app.config['STORAGE_DB'] )
# client[app.config['DB_NAME']].authenticate( app.config['DB_USER'], app.config['DB_PASS'] )
schema_path = app.config['REG_SCHEMA']
if not os.path.exists(schema_path):
    schema_path = os.path.join( sys.prefix, schema_path )
validator = jsonschema.validators.Draft4Validator( json.load(open(schema_path)) )

def log_exception(sender,exception):
    """
    Detailed error logging function.  Designed to attach to Flask exception
    events and logs a bit of extra infomration about the request that triggered
    the exception.

    :param sender: The sender for the exception (we don't use this and log everyhing against app right now)
    :param exception: The exception that was triggered
    :type exception: Exception
    """
    app.logger.error(
        """
Request:   {method} {path}
IP:        {ip}
Raw Agent: {agent}
        """.format(
            method = request.method,
            path = request.path,
            ip = request.remote_addr,
            agent = request.user_agent.string,
        ), exc_info=exception
    )
got_request_exception.connect(log_exception, app)

def jsonp(func):
    """
    A decorator function that wraps JSONified output for JSONP requests.
    """
    @wraps(func)
    def decorated_function(*args, **kwargs):
        callback = request.args.get('callback', False)
        if callback:
            data = str(func(*args, **kwargs).data)
            content = str(callback) + '(' + data + ')'
            mimetype = 'application/javascript'
            return current_app.response_class(content, mimetype=mimetype)
        else:
            return func(*args, **kwargs)
    return decorated_function




class Doc(Resource):
    """
    Container class for all web requests for single documents
    """

    @jsonp
    def get(self, lang, smallkey):
        """
        A REST endpoint for fetching a single document from the doc store.

        :param lang: A two letter language code for the response
        :param smallkey: A short key which uniquely identifies the dataset
        :type smallkey: str
        :returns: Response -- a JSON response object; None with a 404 code if the key was not matched
        """
        doc = db.get_doc( smallkey, lang, self.version )
        print( doc )
        if doc is None:
            return None,404
        return Response(json.dumps(doc),  mimetype='application/json')

class Docs(Resource):
    """
    Container class for all web requests for sets of documents
    """

    @jsonp
    def get(self, lang, smallkeylist, sortarg=''):
        """
        A REST endpoint for fetching a single document from the doc store.

        :param lang: A two letter language code for the response
        :type lang: str
        :param smallkeylist: A comma separated string of short keys each of which identifies a single dataset
        :type smallkeylist: str
        :param sortargs: 'sort' if returned list should be sorted based on geometry
        :type sortargs: str
        :returns: list -- an array of JSON configuration fragments (empty error objects are added where keys do not match)
        """
        keys = [ x.strip() for x in smallkeylist.split(',') ]
        unsorted_docs = [ db.get_doc(smallkey, lang, self.version) for smallkey in keys ]
        if sortarg == 'sort':
            #used to retrieve geometryType
            dbdata = [ db.get_raw(smallkey) for smallkey in keys ]
            lines = []
            polys = []
            points = []
            for rawdata,doc in zip(dbdata, unsorted_docs):
                #Point
                if rawdata["data"]["en"]["geometryType"] == "esriGeometryPoint":
                    points.append(doc)
                #Polygon
                elif rawdata["data"]["en"]["geometryType"] == "esriGeometryPolygon":
                    polys.append(doc)
                #line
                else:
                    lines.append(doc)
            #concat lists (first in docs = bottom of layer list)
            docs = polys + lines + points
        else:
            docs = unsorted_docs
        print( docs )
        return Response(json.dumps(docs),  mimetype='application/json')


class DocV09(Doc):
    def __init__(self):
        super(DocV09,self).__init__()
        self.version = '0.9'

class DocV1(Doc):
    def __init__(self):
        super(DocV1,self).__init__()
        self.version = '1'

class DocsV09(Docs):
    def __init__(self):
        super(DocsV09,self).__init__()
        self.version = '0.9'

class DocsV1(Docs):
    def __init__(self):
        super(DocsV1,self).__init__()
        self.version = '1'

class Register(Resource):
    """
    Container class for all catalog requests for registering new features
    """

    @regparse.sigcheck.validate
    def put(self, smallkey):
        """
        A REST endpoint for adding or editing a single layer.
        All registration requests must contain entries for all languages and will be validated against a JSON schema.

        :param smallkey: A unique identifier for the dataset (can be any unique string, but preferably should be short)
        :type smallkey: str
        :returns: JSON Response -- 201 on success; 400 with JSON payload of an errors array on failure
        """
        try:
            s = json.loads( request.data )
        except Exception:
            return '{"errors":["Unparsable json"]}',400
        if not validator.is_valid( s ):
            resp = { 'errors': [x.message for x in validator.iter_errors(s)] }
            app.logger.info( resp )
            return Response(json.dumps(resp),  mimetype='application/json', status=400)

        data = dict( key=smallkey, request=s )
        try:
            if s['payload_type'] == 'wms':
                data['en'] = regparse.wms.make_node( s['en'], regparse.make_id(smallkey,'en'), app.config )
                data['fr'] = regparse.wms.make_node( s['fr'], regparse.make_id(smallkey,'fr'), app.config )
            else:
                data['en'] = regparse.esri_feature.make_node( s['en'], regparse.make_id(smallkey,'en'), app.config )
                data['fr'] = regparse.esri_feature.make_node( s['fr'], regparse.make_id(smallkey,'fr'), app.config )
        except regparse.metadata.MetadataException as mde:
            app.logger.warning( 'Metadata could not be retrieved for layer', exc_info=mde )
            abort( 400, msg=mde.message )

        app.logger.debug( data )

        db.put_doc( smallkey, { 'type':s['payload_type'], 'data':data } )
        app.logger.info( 'added a smallkey %s' % smallkey )
        return smallkey, 201

    @regparse.sigcheck.validate
    def delete(self, smallkey):
        """
        A REST endpoint for removing a layer.

        :param smallkey: A unique identifier for the dataset
        :type smallkey: str
        :returns: JSON Response -- 204 on success; 500 on failure
        """
        try:
            db.delete_doc( smallkey )
            app.logger.info( 'removed a smallkey %s' % smallkey )
            return '', 204
        except pycouchdb.exceptions.NotFound as nfe:
            app.logger.info( 'smallkey was not found %s' % smallkey,  exc_info=nfe )
        return '',404

class Update(Resource):
    """
    Handles cache maintenance requests
    """

    @regparse.sigcheck.validate
    def post(self, arg):
        """
        A REST endpoint for triggering cache updates.
        Walks through the database and updates cached data.

        :param arg: Either 'all' or a positive integer indicating the minimum
        age in days of a record before it should be updated
        :type arg: str
        :returns: JSON Response -- 200 on success; 400 on malformed URL
        """
        day_limit = None
        try:
            day_limit = int(arg)
        except:
            pass
        if day_limit is None and arg != 'all' or day_limit is not None and day_limit < 1:
            return '{"error":"argument should be either \'all\' or a positive integer"}',400
        return Response( json.dumps( regparse.refresh_records( day_limit, app.config ) ),  mimetype='application/json' )

class Simplification(Resource):
    """
    Handles updates to simplification factor of a feature layer
    """

    @regparse.sigcheck.validate
    def put(self, smallkey):
        """
        A REST endpoint for updating a simplification factor on a registered feature service.

        :param smallkey: A unique identifier for the dataset (can be any unique string, but preferably should be short)
        :type smallkey: str
        :returns: JSON Response -- 200 on success; 400 with JSON payload of an errors array on failure
        """
        try:
            payload = json.loads( request.data )
        except Exception:
            return '{"errors":["Unparsable json"]}',400

        #check that our payload has a 'factor' property that contains an integer
        if not isinstance(payload['factor'], numbers.Integral):
            resp = { 'errors': ['Invalid payload JSON'] }
            app.logger.info( resp )
            return Response(json.dumps(resp),  mimetype='application/json', status=400)

        intFactor = int( payload['factor'] )

        #grab english and french doc fragments
        dbdata = db.get_raw( smallkey )

        if dbdata is None:
            #smallkey/lang is not in the database
            return '{"errors":["Record not found in database"]}',404

        elif dbdata['type'] != 'feature':
            #layer is not a feature layer
            return '{"errors":["Record is not a feature layer"]}',400
        else:
            #add in new simplification factor
            dbdata['data']['en']['maxAllowableOffset'] = intFactor
            dbdata['data']['fr']['maxAllowableOffset'] = intFactor

            #also store factor in the request, so we can preserve the factor during an update
            dbdata['data']['request']['en']['maxAllowableOffset'] = intFactor
            dbdata['data']['request']['fr']['maxAllowableOffset'] = intFactor

        #put back in the database
        db.put_doc( smallkey, { 'type':dbdata['type'], 'data':dbdata['data'] } )

        app.logger.info( 'updated simpification factor on smallkey %(s)s to %(f)d by %(u)s' % {"s":smallkey, "f": intFactor, "u": payload['user'] } )
        return smallkey, 200


global_prefix = app.config.get('URL_PREFIX','')

api_0_9_bp = Blueprint('api_0_9', __name__)
api_0_9 = Api(api_0_9_bp)
api_0_9.add_resource(DocV09, '/doc/<string:lang>/<string:smallkey>')
api_0_9.add_resource(DocsV09, '/docs/<string:lang>/<string:smallkeylist>')
app.register_blueprint(api_0_9_bp, url_prefix=global_prefix+'/v0.9')

api_1_bp = Blueprint('api_1', __name__)
api_1 = Api(api_1_bp)
api_1.add_resource(DocV1, '/doc/<string:lang>/<string:smallkey>')
api_1.add_resource(DocsV1, '/docs/<string:lang>/<string:smallkeylist>', '/docs/<string:lang>/<string:smallkeylist>/<string:sortarg>')
api_1.add_resource(Register, '/register/<string:smallkey>')
api_1.add_resource(Update, '/update/<string:arg>')
api_1.add_resource(Simplification, '/simplification/<string:smallkey>')
app.register_blueprint(api_1_bp, url_prefix=global_prefix+'/v1')

if __name__ == '__main__':
    for l in loggers:
        l.setLevel(0)
        l.info( 'logger started' )
    app.run(debug=True)
