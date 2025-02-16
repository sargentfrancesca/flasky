from flask import render_template, jsonify, request, current_app, url_for, abort
from flask.ext.login import login_required, current_user
from . import api
from .. import db
from flask.ext.login import login_user, logout_user, login_required, \
    current_user
from ..models import User, Species, Population, Taxonomy, Trait, Publication, AuthorContact, AdditionalSource, Stage, StageType, Treatment, MatrixStage, MatrixValue, Matrix, Interval, Fixed, Institute, IUCNStatus, OrganismType, ReproductiveRepetition, GrowthFormRaunkiaer, DicotMonoc, AngioGymno, SpandExGrowthType, SourceType, Database, MissingData, Purpose, ContentEmail, PurposeEndangered, PurposeWeed, Ecoregion, Continent, InvasiveStatusStudy, InvasiveStatusElsewhere, StageTypeClass, TransitionType, MatrixComposition, StartSeason, EndSeason, StudiedSex, Captivity, Status, Version, CensusTiming
from ..decorators import admin_required, permission_required, crossdomain
from .errors import unauthorized, bad_request
import sqlalchemy
import sqlalchemy.ext.declarative as declarative


def key_valid(key):
    users = User.query.all()
    hash = ''
    for user in users:
        if user.api_hash == key:
            hash = key
            request.api_key = key
        else:
            pass

    if hash == key:
        return True
    else:
        return False

@api.route('/')
def home():
    # print vars(request)
    print "HELLO", vars(current_user)
    return render_template('api_1_0/index.html')

@api.route('/docs')
@crossdomain(origin='*')
def docs():
    parent = ['species', 'publications', 'populations', 'studies', 'taxonomies', 'traits', 'additional_sources', 'users', 'matrices', 'stages','versions', 'statuses', 'databases', 'stages', 'matrix_stages', 'matrix_values', 'author_contacts', 'smalls', 'fixed', 'treatments']
    exclude = ['fixed', 'smalls', 'roles', 'matrix_stages', 'stages', 'matrix_values', 'users']
    classes, models, table_names = [], [], []
    for clazz in db.Model._decl_class_registry.values():
        try:
            # print(list(clazz.__table__.columns.keys()))
            table_names.append(clazz.__tablename__)
            classes.append(clazz)
        except:
            pass
    for table in db.metadata.tables.items():
        if table[0] in table_names:
            models.append(classes[table_names.index(table[0])])

    tables_columns = {}
    for model in models:
        if model.__tablename__ not in exclude and model.__tablename__ in parent:
            tables_columns[model.__tablename__] = {k:'' for k, v in model.__table__.columns.items()}
            for key in model.__table__.columns.keys():           
                if len(model.__table__.columns[key].foreign_keys) > 0:
                    table_relation = list(model.__table__.columns[key].foreign_keys)[0]._column_tokens
                    table_name = table_relation[1]
                    table_fk = table_relation[2]
                    # print table_name, table_fk
                    if table_name not in parent:
                        print table_name
                        for m in models:
                            if table_name == m.__tablename__:
                                identifying_variable = m.__table__.columns.items()[1][0]
                                model_queryset = [str(m) for m in m.query.all()]
                                tables_columns[model.__tablename__][table_name] = model_queryset if model_queryset > 0 else None
                                tables_columns[model.__tablename__].pop(key, None)
    
    print tables_columns                        
    schema = tables_columns
    return render_template('api_1_0/schema.html', tables=schema)

''' API query '''
@api.route('/<key>/query/<model>/<int:id>')
@crossdomain(origin='*')
def get_one_entry(key, id, model):
    class_ = False
    
    classes, models, table_names = [], [], []
    for clazz in db.Model._decl_class_registry.values():
        try:
            table_names.append(clazz.__tablename__)
            classes.append(clazz)
        except:
            pass
    for table in db.metadata.tables.items():
        if table[0] in table_names:
            models.append(classes[table_names.index(table[0])])

    for m in models:
        if model == m.__tablename__:
            class_ = m

    if class_:
        entry = class_.query.get_or_404(id)
        if key_valid(key): 
            try:          
                return jsonify(entry.to_json(key))
            except TypeError:
                return entry.to_json(key)

        else:
            unauthorized('Invalid credentials')
    else:
        return bad_request('Bad Request')

@api.route('/<key>/query/<model>/all')
@crossdomain(origin='*')
def get_all_entries(key, model):
    class_ = False
    
    classes, models, table_names = [], [], []
    for clazz in db.Model._decl_class_registry.values():
        try:
            table_names.append(clazz.__tablename__)
            classes.append(clazz)
        except:
            pass
    for table in db.metadata.tables.items():
        if table[0] in table_names:
            models.append(classes[table_names.index(table[0])])

    for m in models:
        if model == m.__tablename__:
            class_ = m

    if class_:
        entries = class_.query.all()
        if key_valid(key):
            try:
                return jsonify({model : [entry.to_json(key) for entry in entries]})
            except TypeError:
                return unauthorized('Invalid Permissions')
        else:
            return unauthorized('Invalid credentials')
    else:
        return bad_request('Bad Request')


''' Filtering '''
def Find(self, **kwargs):
    f = self.db_session.filter(kwargs).all()

@api.route('/<key>/query/<model>/<filters>/all')
@crossdomain(origin='*')
def get_filtered_entries(key, model, filters):
    class_ = False

    kwargs = {}

    terms = filters.split('&')

    for term in terms:
        params = term.split('=')
        kwargs[params[0]] = params[1]
    
    classes, models, table_names = [], [], []
    for clazz in db.Model._decl_class_registry.values():
        try:
            # print(list(clazz.__table__.columns.keys()))
            table_names.append(clazz.__tablename__)
            classes.append(clazz)
        except:
            pass
    for table in db.metadata.tables.items():
        if table[0] in table_names:
            models.append(classes[table_names.index(table[0])])

    for m in models:
        if model == m.__tablename__:
            class_ = m
            # print list(class_.__table__.columns.keys())

    if class_:
        '''Filtering by Meta Table'''
        # Keeping keys from original that align with existing table columns within the parent model
        new_kwargs = {key: value for key, value in kwargs.items() if key in list(class_.__table__.columns.keys())}
        # Storing those that don't match up exactly - these will generally be the foreign keys, although it'll catch typos too
        second = {key: value for key, value in kwargs.items() if key not in list(class_.__table__.columns.keys())}      
        # Manually adding _id as per the syntax of the database schema, as models can't be passed through the URL, but strings can
        relationships = {key+'_id' : value for key, value in second.items() if key+'_id' in list(class_.__table__.columns.keys())}
        #Empty dict for foreign key tables
        fk_tables = {}

        # Getting the actual table names of the meta table options, keeping the value in the dict
        for k, v in relationships.items():
            table_name = list(list(class_.__table__.columns[k].foreign_keys)[0]._column_tokens)[1]
            fk_tables[table_name] = v

        #Iterating through models and fk_tables to match fk_keys table name to actual model object
        for m in models:
            for k, v in fk_tables.items():
                if k == m.__tablename__:  
                    # Grabbing a list of all of the column names                          
                    column_names_list = list(m.__table__.columns.values())
                    column_names = [col.key for col in column_names_list]
                    for name in column_names:
                        # Creating a key word argument to pass through to the query filter, of the column name and the original fk_key value.
                        # Trying each column with the value, to cover names, IDS, code names, etc....
                        kwargs = {name:v}
                        # If a model query matches the parameters
                        if m.query.filter_by(**kwargs).first() != None:
                            fk_model = m.query.filter_by(**kwargs).first()
                            # Grab the model ID
                            fk_model_id = fk_model.id
                            # Go back and loop through the relationships dict to find if substring matches to the original _id column name of the parent model
                            # Add to new_kwargs with the model ID
                            new_kwargs[[ke for ke, va in relationships.items() if k[:-1] in ke][0]] = fk_model_id

        entries = class_.query.filter_by(**new_kwargs).all()
        if key_valid(key):
            try:
                return jsonify({model : [entry.to_json(key) for entry in entries]})
            except TypeError:
                return unauthorized('Invalid Permissions')
        else:
            return unauthorized('Invalid credentials')
    else:
        return bad_request('Bad Request')