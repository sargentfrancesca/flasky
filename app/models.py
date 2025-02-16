from datetime import datetime
import hashlib
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import TimedJSONWebSignatureSerializer as Serializer
from markdown import markdown
import bleach
import json
from flask import Flask, current_app, request, url_for, jsonify, session
from flask.ext.login import UserMixin, AnonymousUserMixin
from app.exceptions import ValidationError
from . import db
from . import login_manager
from sqlalchemy.inspection import inspect

from sqlalchemy import or_
from flask_sqlalchemy import SQLAlchemy, BaseQuery

class VersionQuery(BaseQuery):
    def all_versions(self): #was overwriting the original all() function
        return [s for s in self.filter_by(version_ok=1)]
    def original(self):
        return [s for s in self.filter_by(version_original=1)]
    def latest(self):
        return [s for s in self.filter_by(version_latest=1)]
    def all_checked(self):
        # This is slow
        amber = Status.query.filter(Status.status_name=='Amber').first()
        green = Status.query.filter(Status.status_name=='Green').first()
        return [s for s in self.filter(or_(Version.statuses == amber, Version.statuses == green)).filter(Version.checked == True).order_by(Version.version_number.desc())]
    def all_checked_unchecked(self):
        # This is slow
        amber = Status.query.filter(Status.status_name=='Amber').first()
        green = Status.query.filter(Status.status_name=='Green').first()
        return [s for s in self.filter(or_(Version.statuses == amber, Version.statuses == green)).order_by(Version.version_number.desc())]
    def all_v(self):
        return [s for s in self]
    def version_number(self, id):
        # This has potential to be slow too
        return self.filter(Version.version_number == id).all()

class Permission:
    FOLLOW = 0x01
    COMMENT = 0x02
    WRITE_ARTICLES = 0x04
    MODERATE_COMMENTS = 0x08
    VALIDATION = 0x08
    ADMINISTER = 0x80


class Role(db.Model):
    __tablename__ = 'roles'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True)
    default = db.Column(db.Boolean, default=False, index=True)
    permissions = db.Column(db.Integer)
    users = db.relationship('User', backref='role', lazy='dynamic')

    @staticmethod
    def insert_roles():
        roles = {
            'User': (Permission.FOLLOW |
                     Permission.COMMENT |
                     Permission.WRITE_ARTICLES, True),
            'Developer': (Permission.FOLLOW |
                     Permission.COMMENT |
                     Permission.WRITE_ARTICLES, False),
            'Researcher': (Permission.FOLLOW |
                     Permission.COMMENT |
                     Permission.WRITE_ARTICLES, False),
            'Compadrino': (Permission.FOLLOW |
                     Permission.COMMENT |
                     Permission.WRITE_ARTICLES, False),
            'Committee': (Permission.FOLLOW |
                          Permission.COMMENT |
                          Permission.WRITE_ARTICLES |
                          Permission.MODERATE_COMMENTS |
                          Permission.VALIDATION, False),
            'Moderator': (Permission.FOLLOW |
                          Permission.COMMENT |
                          Permission.WRITE_ARTICLES |
                          Permission.MODERATE_COMMENTS |
                          Permission.VALIDATION, False),
            'Administrator': (0xff, False)
        }
        for r in roles:
            role = Role.query.filter_by(name=r).first()
            if role is None:
                role = Role(name=r)
            role.permissions = roles[r][0]
            role.default = roles[r][1]
            db.session.add(role)
        db.session.commit()

    def __repr__(self):
        return self.name


class User(UserMixin, db.Model):
    #query_class = VersionQuery
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(64), unique=True, index=True)
    username = db.Column(db.String(64), unique=True, index=True)
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'))
    password_hash = db.Column(db.String(128))
    confirmed = db.Column(db.Boolean, default=False)
    name = db.Column(db.String(64))
    location = db.Column(db.String(64))
    about_me = db.Column(db.Text())
    member_since = db.Column(db.Date(), default=datetime.now().date())
    last_seen = db.Column(db.Date(), default=datetime.now().date())
    avatar_hash = db.Column(db.String(32))
    api_hash = db.Column(db.Text())
    tokens = db.Column(db.Text)

    institute_id = db.Column(db.Integer, db.ForeignKey('institutes.id'))
    institute_confirmed = db.Column(db.Boolean, default=False)

    #versions = db.relationship("Version", backref="user")
    
    entered_by = db.relationship("Version", backref="user")
    #checked_by = db.relationship("Version", backref="user")
    changelogger = db.relationship("ChangeLogger", backref="user")
    contacts = db.relationship("AuthorContact", backref="user")

    @staticmethod
    def migrate():
        with open('app/data-migrate/users.json') as user_file:
            data = json.load(user_file)
            user = data["User"]
            usern = user["User"]

           
            for us in usern:
                u = User.query.filter_by(email=us['email']).first()
                if u is None:
                    u = User()
                    

                u.email = us['email']
                u.name = us['name']
                u.username = us['username']
                u.role = Role.query.filter_by(id=us['role_id']).first()
                u.password = generate_password_hash(us['password'])
                u.confirmed = us['confirmed']
                u.institute_id = us['institute_id']
                u.institute_confirmed = us['institute_confirmed']
                

                db.session.add(u)
                db.session.commit()


#    @staticmethod
#    def migrate():
#        Institute.migrate()


    def __init__(self, **kwargs):
        super(User, self).__init__(**kwargs)
        if self.role is None:
            if self.email == current_app.config['FLASKY_ADMIN']:
                self.role = Role.query.filter_by(permissions=0xff).first()
            if self.role is None:
                self.role = Role.query.filter_by(default=True).first()
        if self.email is not None and self.avatar_hash is None:
            self.avatar_hash = hashlib.md5(
                self.email.encode('utf-8')).hexdigest()
        

    @property
    def password(self):
        raise AttributeError('password is not a readable attribute')

    @password.setter
    def password(self, password):
        self.password_hash = generate_password_hash(password)
        username = self.username
        hash_ = hashlib.md5(username).hexdigest()
        self.api_hash = hash_

    def verify_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_confirmation_token(self, expiration=3600):
        s = Serializer(current_app.config['SECRET_KEY'], expiration)
        return s.dumps({'confirm': self.id})

    def confirm(self, token):
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token)
        except:
            return False
        if data.get('confirm') != self.id:
            return False
        self.confirmed = True
        db.session.add(self)
        db.session.commit()
        return True

    def generate_reset_token(self, expiration=3600):
        s = Serializer(current_app.config['SECRET_KEY'], expiration)
        return s.dumps({'reset': self.id})

    def reset_password(self, token, new_password):
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token)
        except:
            return False
        if data.get('reset') != self.id:
            return False
        self.password = new_password
        db.session.add(self)
        db.session.commit()
        return True

    def generate_email_change_token(self, new_email, expiration=3600):
        s = Serializer(current_app.config['SECRET_KEY'], expiration)
        return s.dumps({'change_email': self.id, 'new_email': new_email})

    def change_email(self, token):
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token)
        except:
            return False
        if data.get('change_email') != self.id:
            return False
        new_email = data.get('new_email')
        if new_email is None:
            return False
        if self.query.filter_by(email=new_email).first() is not None:
            return False
        self.email = new_email
        self.avatar_hash = hashlib.md5(
            self.email.encode('utf-8')).hexdigest()
        db.session.add(self)
        db.session.commit()
        return True

    def can(self, permissions):
        return self.role is not None and \
            (self.role.permissions & permissions) == permissions

    def is_administrator(self):
        return self.can(Permission.ADMINISTER)

    def ping(self):
        self.last_seen = datetime.now().date()
        db.session.add(self)

    def gravatar(self, size=100, default='identicon', rating='g'):
        if request.is_secure:
            url = 'https://secure.gravatar.com/avatar'
        else:
            url = 'http://www.gravatar.com/avatar'
        hash = self.avatar_hash or hashlib.md5(
            self.email.encode('utf-8')).hexdigest()
        return '{url}/{hash}?s={size}&d={default}&r={rating}'.format(
            url=url, hash=hash, size=size, default=default, rating=rating)

    def to_json(self, key):
        user = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='users', key=key,
                                      _external=False),
            'data' : {   
                'email' : self.email,         
                'username': self.username,
                'role' : self.role.name,
                'name' : self.name,
                'location' : self.location,
                'about_me' : self.about_me,
                'member_since': self.member_since,
                'last_seen': self.last_seen,
                'institute' : self.institute.to_json_simple(key) if self.institute else None,
                'institute_confirmed' : self.institute_confirmed ,
                'versions' : [version.to_json_simple(key) for version in self.versions]
            }
        }
        return user

    def to_json_simple(self, key):
        user = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='users', key=key,
                                      _external=False),
            'data' : {   
                'email' : self.email,         
                'username': self.username,
                'last_seen': self.last_seen,
                'institute' : self.institute.to_json_simple(key) if self.institute else None,
                'versions_len' : len(self.versions)
            }
        }
        return user

    def generate_auth_token(self):
        username = self.username
        hash_ = hashlib.md5(username).hexdigest()
        self.api_hash = hash_
        db.session.add(self)
        db.session.commit()
        return {'id' : hash_}


    @staticmethod
    def verify_auth_token(token):
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token)
        except:
            return None
        return User.query.get(data['id'])

    def __repr__(self):
        return '<User %r>' % self.username


class AnonymousUser(AnonymousUserMixin):
    def can(self, permissions):
        return False

    def is_administrator(self):
        return False

login_manager.anonymous_user = AnonymousUser


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

''' Start Demog Stuff '''    

''' Meta tables '''

''' Meta Tables for Users '''
class Institute(db.Model):
    __tablename__ = 'institutes'
    id = db.Column(db.Integer, primary_key=True)
    institution_name = db.Column(db.String(400))
    institution_short = db.Column(db.String(100))
    main_contact_email = db.Column(db.String(100))
    main_contact_name = db.Column(db.String(300))
    institution_address = db.Column(db.String(300))
    research_group = db.Column(db.String(300))
    date_joined = db.Column(db.Date(), default=datetime.now().date())
    department = db.Column(db.String(64))
    country = db.Column(db.String(64))
    website = db.Column(db.String(200))
    head_compadrino = db.Column(db.String(64))

    users = db.relationship("User", backref="institute")
    populations = db.relationship("Population", backref="database_source")

    @staticmethod
    def migrate():
        with open('app/data-migrate/users.json') as user_file:
            data = json.load(user_file)
            user = data["User"]
            institute = user["Institute"]

            for ins in institute:
                i = Institute.query.filter_by(institution_name=ins['institution_name']).first()
                if i is None:
                    i = Institute()
                
                i.institution_name = ins['institution_name']
                i.institution_short = ins['institution_short']
                i.main_contact_email = ins['main_contact_email']
                i.main_contact_name = ins['main_contact_name']
                i.institution_address = ins['institution_address']
                i.research_group = ins['research_group']
                i.department = ins['department']
                i.country = ins['country']
                i.website = ins['website']
                i.head_compadrino = ins['head_compadrino']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):        
        institute = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='institutes', key=key,
                                      _external=False),
            'data' :
                {
                    'institution_name': self.institution_name,
                    'institution_short' : self.institution_short,
                    'main_contact_email' : self.main_contact_email,
                    'main_contact_name' : self.main_contact_name,
                    'institution_address' : self.institution_address,
                    'research_group' : self.research_group,
                    'date_joined' : str(self.date_joined),
                    'department' : self.department,
                    'country' : self.country,
                    'website' : self.website,
                    'head_compadrino' : self.head_compadrino,
                    'users' : [user.to_json_simple(key) for user in self.users]}
        }
        return institute

    def to_json_simple(self, key):
        institute = {
        'request_url' : url_for('api.get_one_entry', id=self.id, model='institutes', key=key,
                                  _external=False), 
        'data' : {
            'institution_name': self.institution_name,
            'institution_short' : self.institution_short,
            'main_contact_email' : self.main_contact_email,
            'main_contact_name' : self.main_contact_name,
            'institution_address' : self.institution_address,
            'research_group' : self.research_group,
            'date_joined' : str(self.date_joined),
            'department' : self.department,
            'country' : self.country,
            'website' : self.website,
            'head_compadrino' : self.head_compadrino,
            'users' : len(self.users)}
            
        }
        return institute

    def __repr__(self):
        return self.institution_name

''' End Meta Tables for Users '''

''' Meta Tables for Species '''
class IUCNStatus(db.Model):
    __tablename__ = 'iucn_status'
    id = db.Column(db.Integer, primary_key=True)
    status_code = db.Column(db.String(64), index=True)
    status_name = db.Column(db.String(64))
    status_description = db.Column(db.Text())

    species = db.relationship("Species", backref="iucn_status")

    @staticmethod
    def migrate():
        with open('app/data-migrate/species.json') as species_file:
            data = json.load(species_file)
            species = data["Species"]
            iucn = species["IUCNStatus"]

            for iu in iucn:
                i = IUCNStatus.query.filter_by(status_code=iu['status_code']).first()
                if i is None:
                    i = IUCNStatus()

                i.status_code = iu['status_code']
                i.status_name = iu['status_name']
                i.status_description = iu['status_description']

                db.session.add(i)
                db.session.commit()
            
    def to_json(self, key):
        iucn_status = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='iucn_status', key=key,
                                  _external=False),
            'data' : {
                'status_code': self.status_code,
                'status_name' : self.status_name,
                'status_description' : self.status_description,
                'species' : [species.to_json_simple() for species in self.species]
                }
        }
        return iucn_status

    def to_json_simple(self, key):
        iucn_status = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='iucn_status', key=key,
                                  _external=False),
            'data' : {
                'status_code': self.status_code,
                'status_name' : self.status_name
                }
        }
        return iucn_status

    def __repr__(self):
        return self.status_code


#class ESAStatus(db.Model):
#    __tablename__ = 'esa_statuses'
#    id = db.Column(db.Integer, primary_key=True)
#    status_code = db.Column(db.String(64), index=True, unique=True)
#    status_name = db.Column(db.String(64))
#    status_description = db.Column(db.Text())
#
#    species = db.relationship("Species", backref="esa_status")
#
#    @staticmethod
#    def migrate():
#        with open('app/data-migrate/species.json') as species_file:
#            data = json.load(species_file)
#            species = data["Species"]
#            esa = species["ESAStatus"]
#
#            for ea in esa:
#                i = ESAStatus.query.filter_by(status_code=ea['status_code']).first()
#                if i is None:
#                    i = ESAStatus()
#
#                i.status_code = ea['status_code']
#                i.status_name = ea['status_name']
#
#                db.session.add(i)
#                db.session.commit()
#
#    def to_json(self, key):
#        esa_status = {
#            'request_url' : url_for('api.get_one_entry', id=self.id, model='esa_statuses', key=key,
#                                      _external=False),
#            'data' : {
#                'status_code': self.status_code,
#                'status_name' : self.status_name,
#                'status_description' : self.status_description,
#                'species' : [species.to_json_simple() for species in self.species]
#                }
#
#        }
#        return esa_status
#
#    def to_json_simple(self, key):
#        esa_status = {
#            'request_url' : url_for('api.get_one_entry', id=self.id, model='esa_statuses', key=key,
#                                      _external=False),
#            'data' : {
#                'status_code': self.status_code,
#                'status_name' : self.status_name
#                }
#        }
#        return esa_status
#
#    def __repr__(self):
#        return self.status_code

''' End Meta Tables for Species '''

''' Meta Tables for Taxonomy '''

''' End Meta Tables for Taxonomy '''

''' Meta Tables for Traits '''
class OrganismType(db.Model):
    __tablename__ = 'organism_types'
    id = db.Column(db.Integer, primary_key=True)
    type_name = db.Column(db.String(64), index=True)

    traits = db.relationship("Trait", backref="organism_type")

    @staticmethod
    def migrate():
        with open('app/data-migrate/traits.json') as taxonomy_file:
            data = json.load(taxonomy_file)
            species = data["Trait"]
            growth_types = species["OrganismType"]

            for types in growth_types:
                i = OrganismType.query.filter_by(type_name=types['type_name']).first()
                if i is None:
                    i = OrganismType()

                i.type_name = types['type_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        organism_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='organism_types', key=key,
                                      _external=False),
            'data' : {
                'type_name': self.type_name,
                'traits' : [trait.to_json_simple(key) for trait in self.traits]
            }
        }
        return organism_type

    def to_json_simple(self, key):        
        organism_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='organism_types', key=key,
                                      _external=False),
            'data' : {
                'type_name': self.type_name
            }
        }
        return organism_type

    def __repr__(self):
        return self.type_name

class GrowthFormRaunkiaer(db.Model):
    __tablename__ = 'growth_forms_raunkiaer'
    id = db.Column(db.Integer, primary_key=True, index=True)
    form_name = db.Column(db.Text())

    traits = db.relationship("Trait", backref="growth_form_raunkiaer")

    @staticmethod
    def migrate():
        with open('app/data-migrate/traits.json') as taxonomy_file:
            data = json.load(taxonomy_file)
            species = data["Trait"]
            growth_forms = species["GrowthFormRaunkiaer"]

            for form in growth_forms:
                i = GrowthFormRaunkiaer.query.filter_by(form_name=form['form_name']).first()
                if i is None:
                    i = GrowthFormRaunkiaer()

                i.form_name = form['form_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        growth_form = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='growth_forms_raunkiaer', key=key,
                                      _external=False),
            'data' : {
                'type_name': self.form_name,
                'traits' : [trait.to_json_simple(key) for trait in self.traits]
                }
        }
        return growth_form

    def to_json_simple(self, key):
        growth_form = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='growth_forms_raunkiaer', key=key,
                                      _external=False),
            'data' : {
                'type_name': self.form_name
            }
        }
        return organism_type

    def __repr__(self):
        return self.form_name

class ReproductiveRepetition(db.Model):
    __tablename__ = 'reproductive_repetition'
    id = db.Column(db.Integer, primary_key=True, index=True)
    repetition_name = db.Column(db.Text())

    traits = db.relationship("Trait", backref="reproductive_repetition")

    @staticmethod
    def migrate():
        with open('app/data-migrate/traits.json') as d_file:
            data = json.load(d_file)
            json_data = data["Trait"]
            nodes = json_data["ReproductiveRepetition"]

            for node in nodes:

                i = ReproductiveRepetition.query.filter_by(repetition_name=node['repetition_name']).first()
                if i is None:
                    i = ReproductiveRepetition()

                i.repetition_name = node['repetition_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        reproductive_repetition = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='reproductive_repetition', key=key,
                                      _external=False),
            'data' : {
                'repetition_name': self.repetition_name,
                'traits' : [trait.to_json_simple(key) for trait in self.traits]
                }
        }
        return reproductive_repetition

    def to_json_simple(self, key):
        reproductive_repetition = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='reproductive_repetition', key=key,
                                      _external=False),
            'data' : {
                'repetition_name': self.repetition_name
            }
        }
        return reproductive_repetition

    def __repr__(self):
        return self.repetition_name

class DicotMonoc(db.Model):
    __tablename__ = 'dicot_monoc'
    id = db.Column(db.Integer, primary_key=True)
    dicot_monoc_name = db.Column(db.String(64), index=True)

    traits = db.relationship("Trait", backref="dicot_monoc")

    @staticmethod
    def migrate():
        with open('app/data-migrate/traits.json') as d_file:
            data = json.load(d_file)
            json_data = data["Trait"]
            nodes = json_data["DicotMonoc"]

            for node in nodes:
                i = DicotMonoc.query.filter_by(dicot_monoc_name=node['dicot_monoc_name']).first()
                if i is None:
                    i = DicotMonoc()

                i.dicot_monoc_name = node['dicot_monoc_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        dicot_monoc = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='dicot_monoc', key=key,
                                      _external=False),
            'data' : {
                'dicot_monoc_name': self.dicot_monoc_name,
                'traits' : [trait.to_json_simple(key) for trait in self.traits]
            }
        }
        return dicot_monoc

    def to_json_simple(self, key):
        dicot_monoc = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='dicot_monoc', key=key,
                                      _external=False),
            'data' : {
                'dicot_monoc_name': self.dicot_monoc_name,
            }
        }
        return dicot_monoc

    def __repr__(self):
        return self.dicot_monoc_name

class AngioGymno(db.Model):
    __tablename__ = 'angio_gymno'
    id = db.Column(db.Integer, primary_key=True)
    angio_gymno_name = db.Column(db.String(64), index=True)

    traits = db.relationship("Trait", backref="angio_gymno")

    @staticmethod
    def migrate():
        with open('app/data-migrate/traits.json') as d_file:
            data = json.load(d_file)
            json_data = data["Trait"]
            nodes = json_data["AngioGymno"]

            for node in nodes:
                i = AngioGymno.query.filter_by(angio_gymno_name=node['angio_gymno_name']).first()
                if i is None:
                    i = AngioGymno()

                i.angio_gymno_name = node['angio_gymno_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        angio_gymno = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='angio_gymno', key=key,
                                      _external=False),
            'data' : {
                'angio_gymno_name': self.angio_gymno_name,
                'traits' : [trait.to_json_simple(key) for trait in self.traits]
            }
        }
        return angio_gymno

    def to_json_simple(self, key):
        angio_gymno = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='angio_gymno', key=key,
                                      _external=False),
            'data' : {
                'angio_gymno_name': self.angio_gymno_name
                }
        }
        return angio_gymno

    def __repr__(self):
        return self.angio_gymno_name

class SpandExGrowthType(db.Model):
    __tablename__ = 'spand_ex_growth_types'
    id = db.Column(db.Integer, primary_key=True)
    type_name = db.Column(db.String(64), index=True)
    type_description = db.Column(db.Text)

    traits = db.relationship("Trait", backref="spand_ex_growth_types")

    @staticmethod
    def migrate():

        with open('app/data-migrate/traits.json') as d_file:
            data = json.load(d_file)
            json_data = data["Trait"]
            nodes = json_data["SpandExGrowthType"]

            for node in nodes:          
                i = SpandExGrowthType.query.filter_by(type_name=node['growth_type_name']).first()
                if i is None:
                    i = SpandExGrowthType()

                i.type_name = node['growth_type_name']
                i.type_description = node['growth_type_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        spand_ex_growth_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='spand_ex_growth_types', key=key,
                                      _external=False),
            'data' : {
                'type_name': self.type_name,
                'type_description': self.type_description,
                'traits' : [trait.to_json_simple(key) for trait in self.traits]
            }
        }
        return spand_ex_growth_type

    def to_json_simple(self, key):
        spand_ex_growth_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='spand_ex_growth_types', key=key,
                                      _external=False),
            'data' : {
                'type_name': self.type_name,
                'type_description': self.type_description
                }
        }
        return spand_ex_growth_type

    def __repr__(self):
        return self.type_name
''' End Meta Tables for Traits '''

''' Meta Tables for Publication/Additional Source '''
class SourceType(db.Model):
    __tablename__ = 'source_types'
    id = db.Column(db.Integer, primary_key=True)
    source_name = db.Column(db.String(64), index=True)
    source_description = db.Column(db.Text())

    publications = db.relationship("Publication", backref="source_type")
    additional_sources = db.relationship("AdditionalSource", backref="source_type")

    @staticmethod
    def migrate():
        with open('app/data-migrate/publications.json') as d_file:
            data = json.load(d_file)
            json_data = data["Publication"]
            nodes = json_data["SourceType"]

            for node in nodes:
                i = SourceType.query.filter_by(source_name=node['source_name']).first()
                if i is None:
                    i = SourceType()

                i.source_name = node['source_name']
                i.source_description = node['source_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        source_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='source_types', key=key,
                                      _external=False),
            'data' : {
                'source_name': self.source_name,
                'source_description': self.source_description,
                'publications' : [publication.to_json_simple(key) for publication in self.publications]
            }
        }
        return source_type

    def to_json_simple(self, key):
        source_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='source_types', key=key,
                                      _external=False),
            'data' : {
                'source_name': self.source_name,
                'source_description': self.source_description
            }
        }
        return source_type

    def __repr__(self):
        return self.source_name

##Not sure what this is for, but it was # in the database model below?
    #query_class = VersionQuery
class Database(db.Model):
    __tablename__ = 'databases'
    id = db.Column(db.Integer, primary_key=True)
    database_name = db.Column(db.String(64))
    database_description = db.Column(db.Text())
    database_master_version = db.Column(db.String(64), index=True)
    database_date_created = db.Column(db.Date())
    database_number_species_accepted = db.Column(db.Integer())
    database_number_matrices = db.Column(db.Integer())
    database_agreement = db.Column(db.String(64))

#    database_id = db.Column(db.Integer, db.ForeignKey('databases.id'))
    populations = db.relationship("Population", backref="database")
#    version = db.relationship("Version", backref="database", passive_deletes=True)

###Not sure what these are for....
    #version = db.relationship("Version", backref="database")
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    @staticmethod
    def migrate():
        with open('app/data-migrate/databases.json') as d_file:
            data = json.load(d_file)
            json_data = data["Databasess"]
            nodes = json_data["Database"]

            for node in nodes:
                i = Database.query.filter_by(database_master_version=node['database_master_version']).first()
                if i is None:
                    i = Database()

                i.database_name = node['database_name']
                i.database_description = node['database_description']
                i.database_master_version = node['database_master_version']
                i.database_date_created = node['database_date_created']
                i.database_number_species_accepted = node['database_number_species_accepted']
                i.database_number_matrices = node['database_number_matrices']
                i.database_agreement = node['database_agreement']

                db.session.add(i)
                db.session.commit()


    def to_json(self, key):
        database = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='databases', key=key,
                                      _external=False),
            'data' : {
                'database_name' : self.database_name,
                'database_description' : self.database_description,
                'database_master_version' : self.database_master_version,
                'database_date_created' : self.database_date_created,
                'database_number_species_accepted' : self.database_number_species_accepted,
                'database_number_matrices' : self.database_number_matrices,
                'database_agreement' : self.database_agreement,
                'populations' : [population.to_json(key) for population in self.populations]
#                'versions' : [version.to_json(key) for version in self.versions]
            }
        }
        return database

    def to_json_simple(self, key):
        database = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='databases', key=key,
                                      _external=False),
            'data' : {
                'database_name' : self.database_name,
                'database_description' : self.database_description,
                'database_master_version' : self.database_master_version,
                'database_date_created' : self.database_date_created,
                'database_number_species_accepted' : self.database_number_species_accepted,
                'database_number_matrices' : self.database_number_matrices
            }
        }
        return database

    def __repr__(self):
        return self.database_name

class Purpose(db.Model):
    __tablename__ = 'purposes'
    id = db.Column(db.Integer, primary_key=True)
    purpose_name = db.Column(db.String(64), index=True)
    purpose_description = db.Column(db.Text())

    @staticmethod
    def migrate():
        with open('app/data-migrate/publications.json') as d_file:
            data = json.load(d_file)
            json_data = data["Publication"]
            nodes = json_data["Purpose"]

            for node in nodes:
                i = Purpose.query.filter_by(purpose_name=node['purpose_name']).first()
                if i is None:
                    i = Purpose()

                i.purpose_name = node['purpose_name']
                i.purpose_description = node['purpose_description']

                db.session.add(i)
                db.session.commit()


    def to_json(self, key):
        purpose = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='purposes', key=key,
                                      _external=False),
            'data' : {
                'purpose_name' : self.purpose_name,
                'purpose_description' : self.purpose_description,
                'publications' : [publication.to_json_simple(key) for publication in self.publications]
            }
        }
        return purpose

    def to_json_simple(self, key):
        purpose = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='purposes', key=key,
                                          _external=False),
            'data' : {
                'purpose_name' : self.purpose_name,
                'purpose_description' : self.purpose_description
            }
        }
        return purpose

    def __repr__(self):
        return self.purpose_name

publication_purposes = db.Table('publication_purposes', db.Model.metadata,
    db.Column('id', db.Integer, primary_key=True),
    db.Column('purpose_id', db.Integer, db.ForeignKey('purposes.id', ondelete='CASCADE')),
    db.Column('publication_id', db.Integer, db.ForeignKey('publications.id', ondelete='CASCADE'))
)

class MissingData(db.Model):
    __tablename__ = 'missing_data'
    id = db.Column(db.Integer, primary_key=True)
    missing_code = db.Column(db.String(5), index=True)
    missing_description = db.Column(db.Text())

    @staticmethod
    def migrate():
        with open('app/data-migrate/publications.json') as d_file:
            data = json.load(d_file)
            json_data = data["Publication"]
            nodes = json_data["MissingData"]

            for node in nodes:
                i = MissingData.query.filter_by(missing_code=node['missing_code']).first()
                if i is None:
                    i = MissingData()

                i.missing_code = node['missing_code']
                i.missing_description = node['missing_description']

                db.session.add(i)
                db.session.commit()


    def to_json(self, key):
        missing_data = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='missing_data', key=key,
                                      _external=False),
            'data' : {
                'missing_code' : self.missing_code,
                'missing_description' : self.missing_description,
                'publications' : [publication.to_json_simple(key) for publication in self.publications]
            }
        }
        return missing_data

    def to_json_simple(self, key):
        missing_data = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='missing_data', key=key,
                                      _external=False),
            'data' : {
                'missing_code' : self.missing_code,
                'missing_description' : self.missing_description
            }
        }
        return missing_data

    def __repr__(self):
        return self.missing_code


publication_missing_data = db.Table('publication_missing_data', db.Model.metadata,
    db.Column('id', db.Integer, primary_key=True),
    db.Column('missing_data_id', db.Integer, db.ForeignKey('missing_data.id',ondelete='CASCADE')),
    db.Column('publication_id', db.Integer, db.ForeignKey('publications.id', ondelete='CASCADE'))
)

class PublicationsProtocol (db.Model):
    __tablename__ = 'publications_protocol'
    id = db.Column(db.Integer, primary_key=True)
    protocol_number = db.Column(db.Integer, index=True)
    name = db.Column(db.String(200))
    description = db.Column(db.Text())
    
    publications = db.relationship("Publication", backref = "publications_protocol")

    @staticmethod
    def migrate():
        with open('app/data-migrate/compadrino_protocol.json') as d_file:
            data = json.load(d_file)
            json_data = data["CompadrinoProtocol"]
            nodes = json_data["PublicationsProtocol"]

            for node in nodes:
                i = PublicationsProtocol.query.filter_by(protocol_number=node['protocol_number']).first()
                if i is None:
                    i = PublicationsProtocol()

                i.protocol_number = node['protocol_number']
                i.name = node['name']
                i.description = node['description']

                db.session.add(i)
                db.session.commit()

    def to_json(self,key):
        protocol_number = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model = 'protocol_number', key=key,
                                       _external=False),
            'data' : {
                'protocol_number' : self.protocol_number,
                'name' : self.name,
                'description' : self.description,
                'publications' : [publication.to_json_simple(key) for publication in self.publications]
            }
        }
        return publications_protocol

    def to_json_simple(self, key):
        protocol_number = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='protocol_number', key=key,
                                      _external=False),
            'data' : {
                'protocol_number' : self.protocol_number,
                'description' : self.description
            }
        }
        return publications_protocol

    def __repr__(self):
        return self.protocol_number

''' End Meta Tables for Publication/Additional Source '''

''' Meta Tables for Protocol '''

#class DigitizationProtocol (db.Model):
#    __tablename__ = 'digitization_protocol'
#    id = db.Column(db.Integer, primary_key=True)
#    field_name = db.Column(db.String(50))
#    name_in_csv = db.Column(db.String(100))
#    database_model = db.Column(db.String(50))
#    field_description = db.Column(db.Text())
#    field_short_description = db.Column(db.Text())


class Protocol (db.Model):
    __tablename__ = 'protocol'
    id = db.Column(db.Integer, primary_key=True)
    digitization_protocol_id = db.Column(db.Integer, db.ForeignKey('digitization_protocol.id',ondelete='CASCADE'))
    common_id = db.Column(db.Integer, db.ForeignKey('commonterm.id', ondelete='CASCADE'))

    @staticmethod
    def migrate():
        DigitizationProtocol.migrate()
        CommonUse.migrate()

    def to_json(self,key):
        field_name = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model = 'field_name', key=key,
                                       _external=False),
            'data' : {
                'digitization_protocol' : self.digitization_protocol.to_json_simple(key) if self.digitization_protocol else None,
                'commonly_used' : self.commonly_used.to_json_simple(key) if self.commonly_used else None,
            }
        }
        return protocol

    def __repr__(self):
        return '<Protocol %r>' % self.id

class DigitizationProtocol (db.Model):
    __tablename__ = 'digitization_protocol'
    id = db.Column(db.Integer, primary_key=True)
    field_name = db.Column(db.String(50), index=True)
    name_in_csv = db.Column(db.String(50))
    database_model = db.Column(db.String(50))
    field_description = db.Column(db.Text())
    field_short_description = db.Column(db.Text())

    protocols = db.relationship("Protocol", backref="digitization_protocol")

    @staticmethod
    def migrate():
        with open('app/data-migrate/protocol.json') as d_file:
            data = json.load(d_file)
            json_data = data["Protocolsssss"]
            nodes = json_data["Protocolss"]

            for node in nodes:
                i = DigitizationProtocol.query.filter_by(field_name=node['field_name']).first()
                if i is None:
                    i = DigitizationProtocol()

                i.field_name = node['field_name']
                i.name_in_csv = node['name_in_csv']
                i.database_model = node['database_model']
                i.field_description = node['field_description']
                i.field_short_description = node['field_short_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self,key):
        field_name = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model = 'field_name', key=key,
                                       _external=False),
            'data' : {
                'field_name' : self.field_name,
                'name_in_csv' : self.name_in_csv,
                'database_model' : self.database_model,
                'field_description' : self.field_description,
                'field__short_description' : self.field_short_description,
                'protocols' : [protocol.to_json_simple(key) for protocol in self.protocols]
            }
        }
        return digitization_protocol

    def to_json_simple(self, key):
        field_name = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='field_name', key=key,
                                      _external=False),
            'data' : {
                'field_name' : self.field_name,
                'field__short_description' : self.field__short_description
            }
        }
        return digitization_protocol

    def __repr__(self):
        return self.field_name

class CommonTerm(db.Model):
    __tablename__='commonterm'
    id = db.Column(db.Integer, primary_key=True)
    common_value_name = db.Column(db.String(50), index=True)
    common_value_data = db.Column(db.String(50))
    common_value_description = db.Column(db.String(250))

    protocols = db.relationship("Protocol", backref="commonterm")

    @staticmethod
    def migrate():
        with open('app/data-migrate/protocol.json') as d_file:
            data = json.load(d_file)
            json_data = data["Protocolsssss"]
            nodes = json_data["Common_values"]

            for node in nodes:
                i = CommonTerm.query.filter_by(common_value_name=node['common_value_name']).first()
                if i is None:
                    i = CommonTerm()

                i.common_value_name = node['common_value_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        commonterm = {
        'request_url' : url_for('api.get_one_entry', id=self.id, model='commonterms',key=key,
                                _external=False),
        'data' : {
            'common_value_name' : self.common_value_name,
            'common_value_data' : self.common_value_data,
            'common_value_description' : self.common_value_description,
            'protocols' : [protocol.to_json_simple(key) for protocol in self.protocols]
            }
        }
        return commonterm

    def to_json_simple(self,key):
        commonterm = {
        'request_url' : url_for('api.get_one_entry', id=self.id, model='commonterms',key=key,
                                _external=False),
        'data' : {
            'common_value_name' : self.common_value_name,
            'common_value_description' : self.common_value_description,
            }
        }
        return commonterm

    def __repr__(self):
        return self.common_value_name

''' End Meta Tables for Digitization Protocol '''

''' Upload files '''

#class UploadingFiles (db.Model):
#    __tablename__ = 'uploading files'
#    id = db.Column(db.Integer, primary_key=True)
#    file_title = db.Column(db.String(100))
#    file_description = db.Column(db.String(300))
#    file_filename = db.Column(db.String, default=None, nullable=True)
#    file_url = db.Column(db.String, default=None, nullable=True)
#    is_public = db.Column(db.Boolean, nullable=False)

''' End files '''

''' Meta Tables for Author Contact '''
class ContentEmail(db.Model):
    __tablename__ = 'content_email'
    id = db.Column(db.Integer, primary_key=True)
    content_code = db.Column(db.String(5), index=True)
    content_description = db.Column(db.Text())

    @staticmethod
    def migrate():
        with open('app/data-migrate/author_contacts.json') as d_file:
            data = json.load(d_file)
            json_data = data["AuthorContact"]
            nodes = json_data["ContentEmail"]

            for node in nodes:
                i = ContentEmail.query.filter_by(content_code=node['content_code']).first()
                if i is None:
                    i = ContentEmail()

                i.content_code = node['content_code']
                i.content_description = node['content_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        content_email = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='content_email', key=key,
                                      _external=False),
            'data' : {
                'content_code' : self.content_code,
                'content_description' : self.content_description,
                'author_contacts' : [author_contacts.to_json_simple(key) for author_contacts in self.author_contacts]
            }
        }
        return content_email

    def to_json_simple(self, key):
        content_email = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='content_email', key=key,
                                      _external=False),
            'data' : {
                'content_code' : self.content_code,
                'content_description' : self.content_description
            }
        }
        return content_email

    def __repr__(self):
        return self.content_code

contact_contents = db.Table('contact_contents', db.Model.metadata,
    db.Column('id', db.Integer, primary_key=True),
    db.Column('content_email_id', db.Integer, db.ForeignKey('content_email.id', ondelete='CASCADE')),
    db.Column('author_contact_id', db.Integer, db.ForeignKey('author_contacts.id', ondelete='CASCADE'))
)
''' End Meta Tables for Author Contact '''

''' Meta Tables for Population'''
class PurposeEndangered(db.Model):
    __tablename__ = 'purposes_endangered'
    id = db.Column(db.Integer, primary_key=True)
    purpose_name = db.Column(db.String(64), index=True)
    purpose_description = db.Column(db.Text())

    populations = db.relationship("Population", backref="purpose_endangered")

    @staticmethod
    def migrate():
        with open('app/data-migrate/populations.json') as d_file:
            data = json.load(d_file)
            json_data = data["Population"]
            nodes = json_data["PurposeEndangered"]

            for node in nodes:

                i = PurposeEndangered.query.filter_by(purpose_name=node['purpose_name']).first()

                if i is None:
                    i = PurposeEndangered()

                i.purpose_name = node['purpose_name']
                i.purpose_description = node['purpose_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        purpose_endangered = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='purposes_endangered', key=key,
                                      _external=False),
            'data' : {
                'purpose_name' : self.purpose_name,
                'purpose_description' : self.purpose_description,
                'populations' : [population.to_json_simple(key) for population in self.populations]
            }
        }
        return purpose_endangered

    def to_json_simple(self, key):
        purpose_endangered = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='purposes_endangered', key=key,
                                      _external=False),
            'data' : {
                'purpose_name' : self.purpose_name,
                'purpose_description' : self.purpose_description
            }
        }
        return purpose_endangered

    def __repr__(self):
        return self.purpose_name

class PurposeWeed(db.Model):
    __tablename__ = 'purposes_weed'
    id = db.Column(db.Integer, primary_key=True)
    purpose_name = db.Column(db.String(64), index=True)
    purpose_description = db.Column(db.Text())

    populations = db.relationship("Population", backref="purpose_weed")

    @staticmethod
    def migrate():
        with open('app/data-migrate/populations.json') as d_file:
            data = json.load(d_file)
            json_data = data["Population"]
            nodes = json_data["PurposeWeed"]

            for node in nodes:

                i = PurposeWeed.query.filter_by(purpose_name=node['purpose_name']).first()

                if i is None:
                    i = PurposeWeed()

                i.purpose_name = node['purpose_name']
                i.purpose_description = node['purpose_description']


                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        purpose_weed = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='purposes_weed', key=key,
                                      _external=False),
            'data' : {
                'purpose_name' : self.purpose_name,
                'purpose_description' : self.purpose_description,
                'populations' : [population.to_json_simple(key) for population in self.populations]
            }
        }
        return purpose_weed

    def to_json_simple(self, key):
        purpose_weed = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='purposes_weed', key=key,
                                      _external=False),
            'data' : {
                'purpose_name' : self.purpose_name,
                'purpose_description' : self.purpose_description
            }
        }
        return purpose_weed

    def __repr__(self):
        return self.purpose_name

''' Meta Tables for Population '''
class Ecoregion(db.Model):
    __tablename__ = 'ecoregions'
    id = db.Column(db.Integer, primary_key=True)
    ecoregion_code = db.Column(db.String(5), index=True)
    ecoregion_description = db.Column(db.Text())

    populations = db.relationship("Population", backref="ecoregion")

    @staticmethod
    def migrate():
        with open('app/data-migrate/populations.json') as d_file:
            data = json.load(d_file)
            json_data = data["Population"]
            nodes = json_data["Ecoregion"]

            for node in nodes:
                i = Ecoregion.query.filter_by(ecoregion_code=node['ecoregion_code']).first()
                if i is None:
                    i = Ecoregion()

                i.ecoregion_code = node['ecoregion_code']
                i.ecoregion_description = node['ecoregion_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        ecoregion = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='ecoregions', key=key,
                                      _external=False),
            'data' : {
                'ecoregion_code' : self.ecoregion_code,
                'ecoregion_description' : self.ecoregion_description,
                'populations' : [population.to_json_simple(key) for population in self.populations]
            }
        }
        return ecoregion

    def to_json_simple(self, key):
        ecoregion = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='ecoregions', key=key,
                                      _external=False),
            'data' : {
                'ecoregion_code' : self.ecoregion_code,
                'ecoregion_description' : self.ecoregion_description
            }
        }
        return ecoregion

    def __repr__(self):
        return self.ecoregion_code

class Continent(db.Model):
    __tablename__ = 'continents'
    id = db.Column(db.Integer, primary_key=True)
    continent_name = db.Column(db.String(64), index=True)   
    
    populations = db.relationship("Population", backref="continent")

    @staticmethod
    def migrate():
        with open('app/data-migrate/populations.json') as d_file:
            data = json.load(d_file)
            json_data = data["Population"]
            nodes = json_data["Continent"]

            for node in nodes:
                i = Continent.query.filter_by(continent_name=node['continent_name']).first()
                if i is None:
                    i = Continent()

                i.continent_name = node['continent_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        continent = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='continents', key=key,
                                      _external=False),
            'data' : {
                'continent_name' : self.continent_name,
                'populations' : [population.to_json_simple(key) for population in self.populations] if self.populations else []
            }
        }
        return continent

    def to_json_simple(self, key):
        continent = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='continents', key=key,
                                      _external=False),
            'data' : {
            'continent_name' : self.continent_name
            }
        }
        return continent

    def __repr__(self):
        return self.continent_name

class InvasiveStatusStudy(db.Model):
    __tablename__ = 'invasive_status_studies'
    id = db.Column(db.Integer, primary_key=True)
    status_name = db.Column(db.String(64), index=True)
    status_description = db.Column(db.Text)

    populations = db.relationship("Population", backref="invasive_status_studies")

    @staticmethod
    def migrate():
        with open('app/data-migrate/populations.json') as d_file:
            data = json.load(d_file)
            json_data = data["Population"]
            nodes = json_data["InvasiveStatusStudy"]

            for node in nodes:
                i = InvasiveStatusStudy.query.filter_by(status_name=node['status_name']).first()
                if i is None:
                    i = InvasiveStatusStudy()

                i.status_name = node['status_name']
                i.status_description = node['status_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        invasive_status_study = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='invasive_status_studies', key=key,
                                      _external=False),
            'data' : {
                'status_name' : self.status_name,
                'status_description' : self.status_description,
                'populations' : [population.to_json_simple(key) for population in self.populations]
            }
        }
        return invasive_status_study

    def to_json_simple(self, key):
        invasive_status_study = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='invasive_status_studies', key=key,
                                      _external=False),
            'data' : {
                'status_name' : self.status_name,
                'status_description' : self.status_description
            }
        }
        return invasive_status_study
    def __repr__(self):
        return self.status_name

class InvasiveStatusElsewhere(db.Model):
    __tablename__ = 'invasive_status_elsewhere'
    id = db.Column(db.Integer, primary_key=True)
    status_name = db.Column(db.String(64), index=True)
    status_description = db.Column(db.Text)

    populations = db.relationship("Population", backref="invasive_status_elsewhere")

    @staticmethod
    def migrate():
        with open('app/data-migrate/populations.json') as d_file:
            data = json.load(d_file)
            json_data = data["Population"]
            nodes = json_data["InvasiveStatusElsewhere"]

            for node in nodes:
                i = InvasiveStatusElsewhere.query.filter_by(status_name=node['status_name']).first()
                if i is None:
                    i = InvasiveStatusElsewhere()

                i.status_name = node['status_name']
                i.status_description = node['status_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        invasive_status_elsewhere = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='invasive_status_elsewhere', key=key,
                                      _external=False),
            'data' : {
                'status_name' : self.status_name,
                'status_description' : self.status_description,
                'populations' : [population.to_json_simple(key) for population in self.populations]
            }
        }
        return invasive_status_elsewhere

    def to_json_simple(self, key):
        invasive_status_elsewhere = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='invasive_status_elsewhere', key=key,
                                      _external=False),
            'data' : {
                'status_name' : self.status_name,
                'status_description' : self.status_description
            }
        }
        return invasive_status_elsewhere

    def __repr__(self):
        return self.status_name
''' End Meta Tables for Population '''

''' Meta Tables for Stage Type '''
class StageTypeClass(db.Model):
    __tablename__ = 'stage_type_classes'
    id = db.Column(db.Integer, primary_key=True)
    type_class = db.Column(db.String(64), index=True)

    stage_types = db.relationship("StageType", backref="stage_type_class")

    @staticmethod
    def migrate():
        with open('app/data-migrate/stage_types.json') as d_file:
            data = json.load(d_file)
            json_data = data["StageType"]
            nodes = json_data["StageTypeClass"]

            for node in nodes:
                i = StageTypeClass.query.filter_by(type_class=node['type_class']).first()
                if i is None:
                    i = StageTypeClass()

                i.type_class = node['type_class']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        stage_type_classes = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='stage_type_classes', key=key,
                                      _external=False),
            'data' : {
                'type_class' : self.type_class,
                'stage_types' : [self.stage_types for stage_type in self.stage_types] if self.stage_types else []
            }
        }
        return stage_type_classes

    def to_json_simple(self, key):
        stage_type_classes = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='stage_type_classes', key=key,
                                      _external=False),
            'data' : {
                'type_class' : self.type_class
            }
        }
        return stage_type_classes

    def __repr__(self):
        return self.type_class
''' End Meta Tables for Stage Type '''

''' Meta Tables for MatrixValue '''
class TransitionType(db.Model):
    __tablename__ = 'transition_types'
    id = db.Column(db.Integer, primary_key=True)
    trans_code = db.Column(db.String(64), index=True)
    trans_description = db.Column(db.Text())

    matrix_values = db.relationship("MatrixValue", backref="transition_type")

    @staticmethod
    def migrate():
        with open('app/data-migrate/matrix_values.json') as d_file:
            data = json.load(d_file)
            json_data = data["MatrixValue"]
            nodes = json_data["TransitionType"]

            for node in nodes:
                i = TransitionType.query.filter_by(trans_code=node['trans_code']).first()
                if i is None:
                    i = TransitionType()

                i.trans_code = node['trans_code']
                i.trans_description = node['trans_description']

                db.session.add(i)
                db.session.commit()
    def to_json(self, key):
        transition_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='transition_types', key=key,
                                      _external=False),
            'data' : {
                'trans_code' : self.trans_code,
                'trans_description' : self.trans_description,
                'matrix_values' : [value.to_json_simple() for value in matrix_values] if self.matrix_values else []
            }
        }
        return transition_type

    def to_json_simple(self, key):
        transition_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='transition_types', key=key,
                                      _external=False),
            'data' : {
                'type_class' : self.type_class,
                'trans_description' : self.trans_description
            }
        }
        return transition_type

    def __repr__(self):
        return self.trans_code
''' End Meta Tables for MatrixValue '''

''' Meta Tables for Matrix '''
class MatrixComposition(db.Model):
    __tablename__ = 'matrix_compositions'
    id = db.Column(db.Integer, primary_key=True)
    comp_name = db.Column(db.String(64))
    comp_description = db.Column(db.String(250), index=True)

    matrices = db.relationship("Matrix", backref="matrix_composition")

    @staticmethod
    def migrate():
        with open('app/data-migrate/matrices.json') as d_file:
            data = json.load(d_file)
            json_data = data["Matrix"]
            nodes = json_data["MatrixComposition"]

            for node in nodes:
                i = MatrixComposition.query.filter_by(comp_name=node['comp_name']).first()
                if i is None:
                    i = MatrixComposition()

                i.comp_name = node['comp_name']
                i.comp_description = node['comp_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        matrix_composition = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrix_compositions', key=key,
                                      _external=False),
            'data' : {
                'comp_name' : self.comp_name,
                'comp_description' : self.comp_description,
                'matrices' : [matrix.to_json_simple(key) for matrix in self.matrices]
            }
        }
        return matrix_composition

    def to_json_simple(self, key):
        matrix_composition = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrix_compositions', key=key,
                                      _external=False),
            'data' : {
                'comp_name' : self.comp_name,
                'comp_description' : self.comp_description,
            }
        }
        return matrix_composition

    def __repr__(self):
        return self.comp_name

class StartSeason(db.Model):
    __tablename__ = 'start_seasons'
    id = db.Column(db.Integer, primary_key=True)
    season_id = db.Column(db.Integer())
    season_name = db.Column(db.String(64), index=True)

    matrices = db.relationship("Matrix", backref="start_season", lazy="dynamic")

    @staticmethod
    def migrate():
        with open('app/data-migrate/matrices.json') as d_file:
            data = json.load(d_file)
            json_data = data["Matrix"]
            nodes = json_data["Season"]

            for node in nodes:
                i = StartSeason.query.filter_by(season_id=node['season_id']).first()
                if i is None:
                    i = StartSeason()

                i.season_id = node['season_id']
                i.season_name = node['season_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        start_season = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='start_seasons', key=key,
                                      _external=False),
            'data' : {
                'season_id' : self.season_id,
                'season_name' : self.season_name,
                'matrices' : [matrix.to_json_simple(key) for matrix in self.matrices]
                }
        }
        return start_season

    def to_json_simple(self, key):
        start_season = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='start_seasons', key=key,
                                      _external=False),
            'data' : {
                'season_id' : self.season_id,
                'season_name' : self.season_name
                }
        }
        return start_season

    def __repr__(self):
        return str(self.season_id)

class EndSeason(db.Model):
    __tablename__ = 'end_seasons'
    id = db.Column(db.Integer, primary_key=True)
    season_id = db.Column(db.Integer())
    season_name = db.Column(db.String(64), index=True)

    matrices = db.relationship("Matrix", backref="end_season", lazy="dynamic")

    @staticmethod
    def migrate():
        with open('app/data-migrate/matrices.json') as d_file:
            data = json.load(d_file)
            json_data = data["Matrix"]
            nodes = json_data["Season"]

            for node in nodes:
                i = EndSeason.query.filter_by(season_id=node['season_id']).first()
                if i is None:
                    i = EndSeason()

                i.season_id = node['season_id']
                i.season_name = node['season_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        end_season = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='end_seasons', key=key,
                                      _external=False),
            'data' : {
                'season_id' : self.season_id,
                'season_name' : self.season_name,
                'matrices' : [matrix.to_json_simple(key) for matrix in self.matrices]
                }
        }
        return end_season

    def to_json_simple(self, key):
        end_season = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='end_seasons', key=key,
                                      _external=False),
            'data' : {
                'season_id' : self.season_id,
                'season_name' : self.season_name
            }
        }
        return end_season

    def __repr__(self):
        return str(self.season_id)

class StudiedSex(db.Model):
    __tablename__ = 'studied_sex'
    id = db.Column(db.Integer, primary_key=True)
    sex_code = db.Column(db.String(5), index=True)
    sex_description = db.Column(db.Text())

    matrices = db.relationship("Matrix", backref="studied_sex")

    @staticmethod
    def migrate():
        with open('app/data-migrate/matrices.json') as d_file:
            data = json.load(d_file)
            json_data = data["Matrix"]
            nodes = json_data["StudiedSex"]

            for node in nodes:
                i = StudiedSex.query.filter_by(sex_code=node['sex_code']).first()
                if i is None:
                    i = StudiedSex()

                i.sex_code = node['sex_code']
                i.sex_description = node['sex_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        studied_sex = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='studied_sex', key=key,
                                      _external=False),
            'data' : {
                'sex_code' : self.sex_code,
                'sex_description' : self.sex_description,
                'matrices' : [matrix.to_json_simple(key) for matrix in self.matrices]
                }
        }
        return studied_sex

    def to_json_simple(self, key):
        studied_sex = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='studied_sex', key=key,
                                      _external=False),
            'data' : {
                'sex_code' : self.sex_code,
                'sex_description' : self.sex_description
                }
        }
        return studied_sex

    def __repr__(self):
        return self.sex_code

class Captivity(db.Model):
    __tablename__ = 'captivities'
    id = db.Column(db.Integer, primary_key=True)
    cap_code = db.Column(db.String(5), index=True)
    cap_description = db.Column(db.Text())

    matrices = db.relationship("Matrix", backref="captivities")

    @staticmethod
    def migrate():
        with open('app/data-migrate/matrices.json') as d_file:
            data = json.load(d_file)
            json_data = data["Matrix"]
            nodes = json_data["Captivity"]

            for node in nodes:
                i = Captivity.query.filter_by(cap_code=node['cap_code']).first()
                if i is None:
                    i = Captivity()

                i.cap_code = node['cap_code']
                i.cap_description = node['cap_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        captivity = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='captivities', key=key,
                                      _external=False),
            'data' : {
                'cap_code' : self.cap_code,
                'cap_description' : self.cap_description,
                'matrices' : [matrix.to_json_simple(key) for matrix in self.matrices]
                }
        }
        return captivity

    def to_json_simple(self, key):
        captivity = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='captivities', key=key,
                                      _external=False),
            'data' : {
                'cap_code' : self.cap_code,
                'cap_description' : self.cap_description
                }
        }
        return captivity

    def __repr__(self):
        return self.cap_code


class Status(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'statuses'
    id = db.Column(db.Integer, primary_key=True)
    status_name = db.Column(db.String(64), index=True)
    status_description = db.Column(db.Text())
    notes = db.Column(db.Text())

    version = db.relationship("Version", backref="statuses")
    version_latest = db.Column(db.String(64))
    version_original = db.Column(db.Boolean())
    version_ok = db.Column(db.Boolean)

    @staticmethod
    def migrate():
        with open('app/data-migrate/versions.json') as d_file:
            data = json.load(d_file)
            json_data = data["Version"]
            nodes = json_data["Status"]

            for node in nodes:
                i = Status.query.filter_by(status_name=node['status_name']).first()
                if i is None:
                    i = Status()

                i.status_name = node['status_name']
                i.status_description = node['status_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        status = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='statuses', key=key,
                                      _external=False),
            'data' : {
                'status_name' : self.status_name,
                'status_description' : self.status_description,
                'notes' : self.notes,
                'versions' : [version.to_json_simple(key) for version in self.versions]
            }
        }
        return status

    def to_json_simple(self, key):
        status = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='statuses', key=key,
                                      _external=False),
            'data' : {
                'status_name' : self.status_name,
                'status_description' : self.status_description,
                'notes' : self.notes
            }
        }
        return status

    def __repr__(self):
        return self.status_name
''' End Meta Tables for Matrix '''

''' Meta Tables for Fixed '''
class Small(db.Model):
    __tablename__ = 'smalls'
    id = db.Column(db.Integer, primary_key=True)
    small_name = db.Column(db.String(200), index=True)
    small_description = db.Column(db.Text())

    fixed = db.relationship("Fixed", backref="smalls")

    @staticmethod
    def migrate():
        with open('app/data-migrate/fixed.json') as d_file:
            data = json.load(d_file)
            json_data = data["Fixed"]
            nodes = json_data["Small"]

            for node in nodes:
                i = Small.query.filter_by(small_name=node['small_name']).first()
                if i is None:
                    i = Small()

                i.small_name = node['small_name']
                i.small_description = node['small_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        small = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='smalls', key=key,
                                      _external=False),
            'data' : {
                'small_name' : self.small_name,
                'small_description' : self.small_description,
                'fixed' : [fix.to_json(key) for fix in self.fixed]
            }
        }
        return small

    def to_json_simple(self, key):
        small = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='smalls', key=key,
                                      _external=False),
            'data' : {
                'small_name' : self.small_name,
                'small_description' : self.small_description
            }
        }
        return small

    def __repr__(self):
        return self.small_name

class CensusTiming(db.Model):
    __tablename__ = 'census_timings'
    id = db.Column(db.Integer, primary_key=True)
    census_name = db.Column(db.String(200), index=True)
    census_description = db.Column(db.Text())

    fixed = db.relationship("Fixed", backref="census_timings")

    @staticmethod
    def migrate():
        with open('app/data-migrate/fixed.json') as d_file:
            data = json.load(d_file)
            json_data = data["Fixed"]
            nodes = json_data["CensusTiming"]

            for node in nodes:
                i = CensusTiming.query.filter_by(census_name=node['census_name']).first()
                if i is None:
                    i = CensusTiming()

                i.census_name = node['census_name']
                i.census_description = node['census_description']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        census_timing = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='census_timings', key=key,
                                      _external=False),
            'data' : {
                'census_name' : self.census_name,
                'census_description' : self.census_description,
                'fixed' : [fix.to_json(key) for fix in self.fixed]
                }
        }
        return census_timing

    def to_json_simple(self, key):
        census_timing = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='census_timings', key=key,
                                      _external=False),
            'data' : {
                'census_name' : self.census_name,
                'census_description' : self.census_description
                }
        }
        return census_timing

    def __repr__(self):
        return self.census_name
''' End Meta Tables for Fixed '''

''' End Meta Tables '''

class ChangeLogger(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'changelogger'
    id = db.Column(db.Integer, primary_key=True)
    new_edit_delete = db.Column(db.String(6), default = "edit")
    time_of_edit = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id',ondelete='CASCADE'))
    object_type = db.Column(db.String(64))
    field_name = db.Column(db.String(64))
    object_id = db.Column(db.Integer)
    content_before = db.Column(db.Text)
    content_after = db.Column(db.Text)


class Species(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'species'
    id = db.Column(db.Integer, primary_key=True)
    # subspecies = db.Column(db.String(64))
    species_accepted = db.Column(db.String(64))
    species_common = db.Column(db.String(200))
    iucn_status_id = db.Column(db.Integer, db.ForeignKey('iucn_status.id',ondelete='CASCADE'))
    #esa_status_id = db.Column(db.Integer, db.ForeignKey('esa_statuses.id',ondelete='CASCADE'))
    #invasive_status = db.Column(db.Boolean())
    gbif_taxon_key = db.Column(db.Integer)
    species_iucn_taxonid = db.Column(db.Integer())
    #species_iucn_population_assessed = db.Column(db.String(200))
    image_path = db.Column(db.Text)
    image_path2 = db.Column(db.Text)
    
    taxonomy = db.relationship("Taxonomy", backref="species", passive_deletes=True)
    trait = db.relationship("Trait", backref="species", passive_deletes=True)
    populations = db.relationship("Population", backref="species", passive_deletes=True)

    version = db.relationship("Version", backref="species", uselist=False, passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

#    @staticmethod
#    def find_version(original):
#        original = original[0]
#        last = Version.query.filter_by(original_version_id=original.id).order_by(Version.version_number.desc()).first()
#        return last.version_number + 1
    
    def add_to_logger(self,current_user,field_name,content_before,content_after,new_edit_delete):
        changelogger = {
            'user_id' : current_user.id,
            'object_type' : "species",
            'field_name' : field_name,
            'object_id' : self.id,
            'content_before' : content_before,
            'content_after' : content_after,
            'new_edit_delete' : new_edit_delete
        }
        
        cl = ChangeLogger(**changelogger)
        if cl.content_before != cl.content_after:
            if cl.content_before == None:
                cl.new_edit_delete = "new"

            db.session.add(cl)
            db.session.commit()

#    def save(self,current_user):
#        species = {
#            'species_accepted' : self.species_accepted,
#            'species_common' : self.species_common,
#            'iucn_status_id' : self.iucn_status_id,
#            'esa_status_id' : self.esa_status_id,
#            'species_gisd_status' : self.species_gisd_status,
#            'invasive_status' : self.invasive_status,
#            'gbif_taxon_key' : self.gbif_taxon_key,
#            'species_iucn_taxonid' : self.species_iucn_taxonid,
#            'species_iucn_population_assessed' : self.species_iucn_population_assessed,
#            'image_path' : self.image_path,
#            'image_path2' : self.image_path2,
#            'version_latest' : 1,
#            'version_ok' : 0
#        }
#
#        s = Species(**species)
#        db.session.add(s)
#        db.session.commit() # in the forms this creates 2 copys of everytinh, is this right, who knows?
#
#        status = Status.query.filter_by(status_name="Amber").first() #get an amber status from the metatable
#        
#        try:
#            original_version = self.version.original_version
#            version = {
#            'version_number' : self.find_version(original_version),
#            'original_version_id' : self.version.original_version[0].id,
#            'checked' : 0,
#            'status_id' : status.id,
#            'checked_count' : 0,
#            'version_user_id' : current_user.id, #needs to get user id
#            'database_id' : 1,
#            'species_id' : s.id
#            }   
#        except AttributeError, IntegrityError: # if a previous version doesn't exist: eg. you're creating a new object
#            version = {
#            'version_number' : 1,
#            'original_version_id' : 0, # s: this causes an error because when creating a new version object, this has to have the same original_version_id as it's primary key, so when you commit it gets an int
#            'checked' : 0,
#            'status_id' : status.id,
#            'checked_count' : 0,
#            'version_user_id' : current_user.id, 
#            'database_id' : 1, # what is this
#            'species_id' : s.id
#            }
#            
#        
#        v = Version(**version)
#        db.session.add(v)
#        db.session.flush()
#        if v.original_version_id == 0:
#            v.original_version_id = v.id
#        db.session.commit()
#
#        return s
#
#    def save_admin(self, current_user):
#        species = {
#            'species_accepted' : self.species_accepted,
#            'species_common' : self.species_common,
#            'iucn_status_id' : self.iucn_status_id,
#            'esa_status_id' : self.esa_status_id,
#            'species_gisd_status' : self.species_gisd_status,
#            'invasive_status' : self.invasive_status,
#            'gbif_taxon_key' : self.gbif_taxon_key,
#            'species_iucn_taxonid' : self.species_iucn_taxonid,
#            'species_iucn_population_assessed' : self.species_iucn_population_assessed,
#            'image_path' : self.image_path,
#            'image_path2' : self.image_path2,
#            'version_latest' : 1,
#            'version_ok' : 1
#        }
#
#        s = Species(**species)
#        db.session.add(s)
#        db.session.commit()
#
#        status = Status.query.filter_by(status_name="Green").first()
#        original_version = self.version.original_version
#
#        version = {
#            'version_number' : self.find_version(original_version),
#            'original_version_id' : self.version.original_version[0].id,
#            'checked' : 1,
#            'status_id' : status.id,
#            'checked_count' : self.version.checked_count + 1 if self.version.checked_count else 1,
#            'version_user_id' : current_user.id,
#            'database_id' : 1,
#            'species_id' : s.id
#        }
#
#        v = Version(**version)
#        db.session.add(v)
#        db.session.commit()
#
#        original_version_children = self.version.original_version[0].child_versions
#
#        for child in original_version_children:
#            child.version_latest = 0
#            db.session.add(child)
#            db.session.commit()
#
#        return s

    # def verify(self, status_colour, current_user):
    #     status = Status.query.filter_by(status_name=status_colour).first()

    #     self.version.status_id = status.id
    #     self.version.checked = True
    #     self.version.checked_count = self.version.checked_count + 1 if self.version.checked_count else 1
    #     self.version.version_user_id = current_user.id

    #     this_version = [self.version.version_number]

    #     if status.status_name == 'Green':
    #         self.version_latest = 1
    #         self.version_ok = 1
    #         original_version_children = self.version.original_version[0].child_versions
    #         for child in original_version_children:
    #             child.version_latest = 0
    #             db.session.add(child)
    #             db.session.commit()
        
    #     if status.status_name == 'Amber':
    #         self.version_latest = 0
    #         self.version_ok = 0
    #         original_version_children = self.version.original_version[0].child_versions
    #         original_species_children = [s.species for s in self.version.original_version[0].child_versions]
    #         green = Status.query.filter_by(status_name='Green').first()
    #         this_version = [v.version_number]
    #         green_statuses = [child.version_number for child in original_version_children if child.statuses is green and child.version_number not in this_version]
    #         closest_green_status = min(green_statuses, key=lambda x:abs(x-this_version[0]))
    #         closest_id = [child for child in original_version_children if child.version_number is closest_green_status][0].species_id
    #         species = Species.query.get(closest_id)
    #         species.version_latest = 1
 
    @staticmethod
    def migrate():
        IUCNStatus.migrate()

    def to_json(self, key):
        species = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='species', key=key,
                                      _external=False),
            'data' : {
                'species_accepted': self.species_accepted,
                'species_common': self.species_common,
                'taxonomy' : [taxonomy.to_json_simple(key) for taxonomy in self.taxonomy][0],
                'traits' : [trait.to_json_simple(key) for trait in self.trait],
                'populations' : [population.to_json_simple(key) for population in self.populations],
                'number_populations' : len([population.to_json_simple(key) for population in self.populations])
            }
        }

        user = User.query.filter_by(api_hash=key).first()
        if user is not None and user.institute.institution_name == "University of Exeter":
            species['data']['gbif_taxon_key'] = self.gbif_taxon_key
            species['data']['iucn_status'] = self.iucn_status.to_json_simple(key) if self.iucn_status else None
            species['data']['esa_status'] = self.esa_status.to_json_simple(key) if self.esa_status else None
        
        return species

    def to_json_simple(self, key):
        species = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='species', key=key,
                                      _external=False),
            'data' : {
                'species_accepted': self.species_accepted,
                'populations_len' : len(self.populations),
                #'versions' : len(self.versions)
            }
        }
        return species


    def __repr__(self):
        return '<Species %r>' % self.id

class Taxonomy(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'taxonomies'
    id = db.Column(db.Integer, primary_key=True)
    species_id = db.Column(db.Integer, db.ForeignKey('species.id',ondelete='CASCADE'))
    authority = db.Column(db.Text())
    tpl_version = db.Column(db.String(64)) # Currently at 1.0, which could be float, but sometimes releases are 1.0.1 etc, best as string for now?
    infraspecies_accepted = db.Column(db.String(64))
    species_epithet_accepted = db.Column(db.String(64))
    genus_accepted = db.Column(db.String(64))
    genus = db.Column(db.String(64))
    family = db.Column(db.String(64))
    tax_order = db.Column(db.String(64))
    tax_class = db.Column(db.String(64))
    phylum = db.Column(db.String(64))
    kingdom = db.Column(db.String(64))

    col_check_ok = db.Column(db.Boolean())
    col_check_date = db.Column(db.Date())

    version = db.relationship("Version", backref="taxonomy", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    @staticmethod
    def migrate():
        pass

    def to_json(self, key):
        taxonomy = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='taxonomies', key=key,
                                      _external=False),
            'data' : {
                'species' : self.species.to_json_simple(key),
                'publication' : self.publication.to_json_simple(key),
                #'species_author' : self.species_author,
                'authority' : self.authority,
                'tpl_version' : self.tpl_version,
                'infraspecies_accepted' : self.infraspecies_accepted,
                'species_epithet_accepted' : self.species_epithet_accepted,
                'genus_accepted' : self.genus_accepted,
                'genus' : self.genus,
                'family' : self.family,
                'tax_order' : self.tax_order,
                'tax_class' : self.tax_class,
                'phylum' : self.phylum,
                'kingdom' : self.kingdom,
                'col_check_ok' : self.col_check_ok,
                'col_check_date' : self.col_check_date,
                'versions' : [version.to_json_simple(key) for version in self.versions]
            }
        }
        return taxonomy

    def to_json_simple(self, key):
        taxonomy = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='taxonomies', key=key,
                                      _external=False),
            'data' : {
                'authority' : self.authority,
                'genus' : self.genus,
                'family' : self.family,
            }
        }
        return taxonomy

    def __repr__(self):
        return '<Taxonomy %r>' % self.id
    
    def add_to_logger(self,current_user,field_name,content_before,content_after,new_edit_delete):
        changelogger = {
            'user_id' : current_user.id,
            'object_type' : "taxonomy",
            'field_name' : field_name,
            'object_id' : self.id,
            'content_before' : content_before,
            'content_after' : content_after,
            'new_edit_delete' : new_edit_delete
        }
        
        cl = ChangeLogger(**changelogger)
        if cl.content_before != cl.content_after:
            if cl.content_before == None:
                cl.new_edit_delete = "new"

            db.session.add(cl)
            db.session.commit()


class Trait(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'traits'
    id = db.Column(db.Integer, primary_key=True)
    species_id = db.Column(db.Integer, db.ForeignKey('species.id',ondelete='CASCADE'))
    #max_height = db.Column(db.Float()) #This should be a double, eventually
    organism_type_id = db.Column(db.Integer, db.ForeignKey('organism_types.id',ondelete='CASCADE'))
    growth_form_raunkiaer_id = db.Column(db.Integer, db.ForeignKey('growth_forms_raunkiaer.id',ondelete='CASCADE')) 
    reproductive_repetition_id = db.Column(db.Integer, db.ForeignKey('reproductive_repetition.id',ondelete='CASCADE'))
    dicot_monoc_id = db.Column(db.Integer, db.ForeignKey('dicot_monoc.id',ondelete='CASCADE'))
    angio_gymno_id = db.Column(db.Integer, db.ForeignKey('angio_gymno.id',ondelete='CASCADE'))
    spand_ex_growth_type_id = db.Column(db.Integer, db.ForeignKey('spand_ex_growth_types.id',ondelete='CASCADE'))
    species_seedbank = db.Column(db.Boolean())
    species_clonality = db.Column(db.Boolean())
    species_gisd_status = db.Column(db.Boolean())
    species_seedbank_source = db.Column(db.Text())
    species_clonality_source = db.Column(db.Text())

    version = db.relationship("Version", backref="trait", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    @staticmethod
    def migrate():
        OrganismType.migrate()
        GrowthFormRaunkiaer.migrate()
        ReproductiveRepetition.migrate()
        DicotMonoc.migrate()
        AngioGymno.migrate()
        SpandExGrowthType.migrate()

    def to_json(self, key):
        trait = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='traits', key=key,
                                      _external=False),
            'data' : {
                'species' : self.species.to_json_simple(key) if self.species else None,
                'organism_type' : self.organism_type.to_json_simple(key) if self.organism_type else None,
                'reproductive_repetition' : self.reproductive_repetition.to_json_simple(key) if self.reproductive_repetition else None,
                'dicot_monoc' : self.dicot_monoc.to_json_simple(key) if self.dicot_monoc else None,
                'angio_gymno' : self.angio_gymno.to_json_simple(key) if self.angio_gymno else None,
                'species_seedbank': self.species_seedbank,
                'species_clonality': self.species_clonality,
                'species_gisd_status': self.species_gisd_status,
                
                'versions' : [version.to_json_simple(key) for version in self.versions]
                }
        }

        user = User.query.filter_by(api_hash=key).first()
        if user is not None and user.institute.institution_name == "University of Exeter":
            trait['data']['growth_form_raunkiaer'] = self.growth_form_raunkiaer.to_json_simple(key) if self.growth_form_raunkiaer else None
            trait['data']['spand_ex_growth_type'] = self.spand_ex_growth_types.to_json_simple(key) if self.spand_ex_growth_types else None
        
        return trait

    def to_json_simple(self, key):
        trait = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='traits', key=key,
                                      _external=False),
            'data' : {
                'species' : self.species.species_accepted if self.species else None,
                'organism_type' : self.organism_type.to_json_simple(key) if self.organism_type else None,
                'reproductive_repetition' : self.reproductive_repetition.to_json_simple(key) if self.reproductive_repetition else None,
                'dicot_monoc' : self.dicot_monoc.to_json_simple(key) if self.dicot_monoc else None,
                'angio_gymno' : self.angio_gymno.to_json_simple(key) if self.angio_gymno else None,
                }
        }

        user = User.query.filter_by(api_hash=key).first()
        if user is not None and user.institute.institution_name == "University of Exeter":
            trait['data']['growth_form_raunkiaer'] = self.growth_form_raunkiaer.to_json_simple(key) if self.growth_form_raunkiaer else None
            trait['data']['spand_ex_growth_type'] = self.spand_ex_growth_types.to_json_simple(key) if self.spand_ex_growth_types else None
        
        return trait

    def __repr__(self):
        return '<Trait %r>' % self.id
    
    def add_to_logger(self,current_user,field_name,content_before,content_after,new_edit_delete):
        changelogger = {
            'user_id' : current_user.id,
            'object_type' : "trait",
            'field_name' : field_name,
            'object_id' : self.id,
            'content_before' : content_before,
            'content_after' : content_after,
            'new_edit_delete' : new_edit_delete
        }
        
        cl = ChangeLogger(**changelogger)
        if cl.content_before != cl.content_after:
            if cl.content_before == None:
                cl.new_edit_delete = "new"

            db.session.add(cl)
            db.session.commit()

class Publication(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'publications'
    id = db.Column(db.Integer, primary_key=True)
    source_type_id = db.Column(db.Integer, db.ForeignKey('source_types.id',ondelete='CASCADE'))
    authors = db.Column(db.Text()) # These appear as vectors in Judy's schema, trying to think of the best way to implement this within MySQL and Django/Flask
    editors = db.Column(db.Text())
    pub_title = db.Column(db.Text())
    journal_book_conf = db.Column(db.Text())
    year = db.Column(db.SmallInteger()) #proto
    volume = db.Column(db.Text())
    pages = db.Column(db.Text())
    publisher = db.Column(db.Text())
    city = db.Column(db.Text())
    country = db.Column(db.Text())
    institution = db.Column(db.Text())
    DOI_ISBN = db.Column(db.Text())
    journal_name = db.Column(db.Text()) #r-generated, needs more info, probably generated in method of this model
    purposes = db.relationship("Purpose",
                    secondary=publication_purposes, backref="publications", passive_deletes=True)
    date_digitised = db.Column(db.DateTime(), default=datetime.now().date())
    embargo = db.Column(db.Date()) #nullable
    missing_data = db.relationship("MissingData",
                    secondary=publication_missing_data, backref="publications", passive_deletes=True)
    additional_source_string = db.Column(db.Text())
    colour = db.Column(db.String(7))
    
    study_notes = db.Column(db.Text())
    publications_protocol_id = db.Column(db.Integer, db.ForeignKey('publications_protocol.id',ondelete='CASCADE'))

    # Establishing one to many relationships between tables
    author_contacts = db.relationship("AuthorContact", backref="publication", passive_deletes=True)
    additional_sources = db.relationship("AdditionalSource", backref="publication", passive_deletes=True)
    populations = db.relationship("Population", backref="publication", passive_deletes=True)
    
    

    version = db.relationship("Version", backref="publication", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    @staticmethod
    def migrate():
        SourceType.migrate()
        Purpose.migrate()
        MissingData.migrate()
        PublicationsProtocol.migrate()

    def to_json(self, key):
        publication = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='publications', key=key,
                                      _external=False),
            'data' : {
                'source_type' : self.source_type.to_json_simple(key) if self.source_type else None,
                'authors' : self.authors,
                'editors' : self.editors,
                'pub_title' : self.pub_title,
                'journal_book_conf' : self.journal_book_conf,
                'year' : self.year,
                'volume' : self.volume,
                'pages' : self.pages,
                'publisher' : self.publisher,
                'city' : self.city,
                'country' : self.country,
                'institution' : self.institution,
                'DOI_ISBN' : self.DOI_ISBN,
                'journal_name' : self.journal_name,
                'corresponding_author' : self.corresponding_author,
                'email' : self.email,
                'purposes' : self.purpose.to_json_simple(key) if self.purpose else None,
                'date_digitised' : self.date_digitised,
                'embargo' : self.embargo,
                'missing_data' : self.missing_data.to_json_simple(key) if self.purposes else None,
                'additional_source_string' : self.additional_source_string,
                'author_contacts' : self.author_contacts.to_json_simple(key) if self.author_contacts else None,
                'additional_sources' : self.additional_sources.to_json_simple(key) if self.additional_sources else None,
                'populations' : [populations.to_json_simple(key) for populations in self.populations],
                'stages' : [stages.to_json_simple(key) for stages in self.stages] if self.stages else None,
                'taxonomies' : [taxonomies.to_json_simple(key) for taxonomies in self.taxonomies],
                'publications_protocol' : self.publications_protocol.to_json_simple(key) if self.publications_protocol else None,
#                'studies' : [studies.to_json_simple(key) for studies in self.studies] if self.studies else None,
                'versions' : [versions.to_json_simple(key) for versions in self.versions] if self.versions else None
            }
        }
        return publication

    def to_json_simple(self, key):
        publication = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='publications', key=key,
                                      _external=False),
            'data' : {
                'source_type' : self.source_type.to_json_simple(key) if self.source_type else None,
                'authors' : self.authors,
                'pub_title' : self.pub_title,
                'year' : self.year,
                'DOI_ISBN' : self.DOI_ISBN,
                'journal_name' : self.journal_name,
                #'purposes' : self.purposes.to_json_simple(key) if self.purposes else None,
                'date_digitised' : self.date_digitised,
                'embargo' : self.embargo,
                'additional_source_string' : self.additional_source_string,
                'additional_sources_len' : len(self.additional_sources),
                'populations_len' : len(self.populations)
                #'versions_len' : len(self.versions)
            }
        }
        return publication

    def __repr__(self):
        return '<Publication %r>' % self.id
    
    def add_to_logger(self,current_user,field_name,content_before,content_after,new_edit_delete):
        changelogger = {
            'user_id' : current_user.id,
            'object_type' : "publication",
            'field_name' : field_name,
            'object_id' : self.id,
            'content_before' : content_before,
            'content_after' : content_after,
            'new_edit_delete' : new_edit_delete
        }
        
        cl = ChangeLogger(**changelogger)
        if cl.content_before != cl.content_after:
            if cl.content_before == None:
                cl.new_edit_delete = "new"

            db.session.add(cl)
            db.session.commit()
            
class AuthorContact(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'author_contacts'
    id = db.Column(db.Integer, primary_key=True)
    publication_id = db.Column(db.Integer, db.ForeignKey('publications.id',ondelete='CASCADE'))
    corresponding_author = db.Column(db.Text())
    corresponding_author_email = db.Column(db.Text())
    date_contacted = db.Column(db.Date(), index=True)
    date_contacted_again = db.Column(db.Date(), index=True)
    contacting_user_id = db.Column(db.Integer, db.ForeignKey('users.id',ondelete='CASCADE'))
    content_emails = db.relationship("ContentEmail",
                    secondary=contact_contents, backref="author_contacts", passive_deletes=True)
    extra_content_email = db.Column(db.Text())
    correspondence_email_content = db.Column(db.Text())
    author_reply = db.Column(db.Text())

    version = db.relationship("Version", backref="author_contact", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)


    @staticmethod
    def migrate():
        ContentEmail.migrate()

    def to_json(self, key):
        author_contact = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='author_contacts', key=key,
                                      _external=False),
            'data' : {
                'publication' : self.publication.to_json_simple(key) if self.publication else None,
                'corresponding_author' : self.corresponding_author,
                'corresponding_author_email' : self.corresponding_author_email,
                'date_contacted' : self.date_contacted,
                'contacting_user' : self.contacting_user.to_json_simple(key) if self.contacting_user else None,
                'content_email_id' : self.content_email.to_json_simple(key) if self.content_email else None,
                'content_email_text' : self.content_email_text,
                'author_reply' : self.author_reply,
            }
        }
        return author_contact

    def to_json_simple(self, key):
        author_contact = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='author_contacts', key=key,
                                      _external=False),
            'data' : {
                'publication_len' : len(self.publication),
                'date_contacted' : self.date_contacted,
                'author_reply' : self.author_reply,
            }
        }
        return author_contact

    def __repr__(self):
        return str(self.publication_id)

class AdditionalSource(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'additional_sources'
    id = db.Column(db.Integer, primary_key=True)
    publication_id = db.Column(db.Integer, db.ForeignKey('publications.id',ondelete='CASCADE'))
    source_type_id = db.Column(db.Integer, db.ForeignKey('source_types.id',ondelete='CASCADE'))
    authors = db.Column(db.Text())
    editors = db.Column(db.Text())
    pub_title = db.Column(db.Text())
    journal_book_conf = db.Column(db.Text())
    year = db.Column(db.SmallInteger()) #proto
    volume = db.Column(db.Text())
    pages = db.Column(db.Text())
    publisher = db.Column(db.Text())
    city = db.Column(db.Text())
    country = db.Column(db.Text())
    institution = db.Column(db.Text())
    DOI_ISBN = db.Column(db.Text())
    journal_name = db.Column(db.Text()) #r-generated, needs more info, probably to be generated in method of this model, first author in author list?
    description = db.Column(db.Text())

    version = db.relationship("Version", backref="additional_source", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    def to_json(self, key):
        additional_source = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='additional_sources', key=key,
                                      _external=False),
            'data' : {
                'source_type' : self.source_type.to_json_simple(key),
                'contacting_user_id' : self.contacting_user.to_json_simple(key),
                'authors' : self.authors,
                'editors' : self.editors,
                'pub_title' : self.pub_title,
                'journal_book_conf' : self.journal_book_conf,
                'year' : self.year,
                'volume' : self.volume,
                'pages' : self.pages,
                'publisher' : self.publisher,
                'city' : self.city,
                'country' : self.country,
                'institution' : self.institution,
                'DOI_ISBN' : self.DOI_ISBN,
                'journal_name' : self.journal_name,
                'description' : self.description,
                'versions' : [version.to_json_simple(key) for version in versions]
            }
        }
        return additional_source

    def to_json_simple(self, key):
        additional_source = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='additional_sources', key=key,
                                      _external=False),
            'data' : {
                'authors' : self.authors,
                'pub_title' : self.pub_title,
                'year' : self.year,
                'DOI_ISBN' : self.DOI_ISBN,
                'journal_name' : self.journal_name,
                'description' : self.description,
                'versions_len' : len(self.versions)
            }
        }
        return additional_source

    def __repr__(self):
        return '<Additional Source %r>' % self.id

class Population(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'populations'
    id = db.Column(db.Integer, primary_key=True, index=True)
    publication_id = db.Column(db.Integer, db.ForeignKey('publications.id',ondelete='CASCADE'))
    species_id = db.Column(db.Integer, db.ForeignKey('species.id',ondelete='CASCADE'))
    species_author = db.Column(db.String(64))
    population_name = db.Column(db.Text())
    ecoregion_id = db.Column(db.Integer, db.ForeignKey('ecoregions.id',ondelete='CASCADE'))
    ###Danny adding database stuff
    database_id = db.Column(db.Integer, db.ForeignKey('databases.id',ondelete='CASCADE'))
    invasive_status_study_id = db.Column(db.Integer, db.ForeignKey('invasive_status_studies.id',ondelete='CASCADE')) #
    invasive_status_elsewhere_id = db.Column(db.Integer, db.ForeignKey('invasive_status_elsewhere.id',ondelete='CASCADE')) #
    country = db.Column(db.Text())
    population_nautical_miles = db.Column(db.Integer())
    continent_id = db.Column(db.Integer, db.ForeignKey('continents.id',ondelete='CASCADE'))
    
    latitude = db.Column(db.Float())
    longitude = db.Column(db.Float())
    
    lat_ns = db.Column(db.String(1))
    lat_deg = db.Column(db.Integer())
    lat_min = db.Column(db.Integer())
    lat_sec = db.Column(db.Integer())
    
    lon_ew = db.Column(db.String(1))
    lon_deg = db.Column(db.Integer())
    lon_min = db.Column(db.Integer())
    lon_sec = db.Column(db.Integer())
        
    altitude = db.Column(db.Float())
    #pop_size = db.Column(db.Text())
    within_site_replication = db.Column(db.Text())
    
    #from study
    study_duration = db.Column(db.Integer(), index=True)
    study_start = db.Column(db.Integer())
    study_end = db.Column(db.Integer())

    purpose_endangered_id = db.Column(db.Integer, db.ForeignKey('purposes_endangered.id',ondelete='CASCADE'))
    purpose_weed_id = db.Column(db.Integer, db.ForeignKey('purposes_weed.id',ondelete='CASCADE'))

    database_source_id = db.Column(db.Integer, db.ForeignKey('institutes.id',ondelete='CASCADE'))

    number_populations = db.Column(db.Integer())
    #from study end
    
    matrices = db.relationship("Matrix", backref="population", passive_deletes=True)

    version = db.relationship("Version", backref="population", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

#    def geometries_dec(self):
#        geo = json.loads(self.geometries)
#
#        lat_min = geo['lat_min']
#        lat_deg = geo['lat_deg']
#        lat_sec = geo['lat_sec']
#        lat_ns = geo['lat_ns']
#        lon_min = geo['lon_min']
#        lon_deg = geo['lon_deg']
#        lon_sec = geo['lon_sec']
#        lat_we = geo['lat_we']
#        altitude = geo['altitude']
#
#        if lat_we != 'NA':
#            if lat_we == 'W':
#                lon_deg = -float(lon_deg)
#                
#        try:
#            decimal_lat = float(lat_deg)+float(lat_min)/60+float(lat_sec)/3600
#            decimal_lon = float(lon_deg)+float(lon_min)/60+float(lon_sec)/3600
#            altitude = float(altitude)
#        except:
#            decimal_lat = 'NA'
#            decimal_lon = 'NA'
#            altitude = 'NA'
#
#        geometries = {"latitude" : decimal_lat, "longitude" : decimal_lon, "altitude" : altitude}
#        return geometries


    def to_json(self, key):
        population = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='populations', key=key,
                                      _external=False),
            'data' : {
                'species' : self.species.to_json_simple(key),
                'publication' : self.publication.to_json_simple(key),
                'species_author' : self.species_author,
                'population_name' : self.population_name,
                'ecoregion' : self.ecoregion.to_json_simple(key) if self.ecoregion else None,
                'country' : self.country,
                'database' : self.database.to_json_simple(key) if self.database else None,
                'population_nautical_miles' : self.population_nautical_miles,
                'continent' : self.continent.to_json_simple(key) if self.continent else None,
                'longitude' : self.longitude,
                'latitude' : self.latitude,
                'altitude' : self.altitude,
                'matrices' : [matrix.to_json_simple(key) for matrix in self.matrices] if self.matrices else None#,
                #'versions' : [version.to_json_simple(key) for version in self.versions] if self.versions else None,
            }
           
        }
        user = User.query.filter_by(api_hash=key).first()
        if user is not None and user.institute.institution_name == "University of Exeter":  
            population['data']['invasive_status_study'] = self.invasive_status_studies.to_json_simple(key) if self.invasive_status_studies else None
            population['data']['invasive_status_elsewhere'] = self.invasive_status_elsewhere.to_json_simple(key) if self.invasive_status_elsewhere else None
            population['data']['population_size'] = self.pop_size
        
        return population

    def to_json_simple(self, key):
        population = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='populations', key=key,
                                      _external=False),
            'data' : {
                'population_name' : self.population_name,
                'ecoregion' : self.ecoregion.to_json_simple(key) if self.ecoregion else None,
                'country' : self.country,
                'matrices_len' : len(self.matrices),
            }
           
        }

        return population
    
    def add_to_logger(self,current_user,field_name,content_before,content_after,new_edit_delete):
        changelogger = {
            'user_id' : current_user.id,
            'object_type' : "population",
            'field_name' : field_name,
            'object_id' : self.id,
            'content_before' : content_before,
            'content_after' : content_after,
            'new_edit_delete' : new_edit_delete
        }
        
        cl = ChangeLogger(**changelogger)
        if cl.content_before != cl.content_after:
            if cl.content_before == None:
                cl.new_edit_delete = "new"

            db.session.add(cl)
            db.session.commit()


    @staticmethod
    def migrate():
        Ecoregion.migrate()
        Continent.migrate()
        Database.migrate()
        InvasiveStatusStudy.migrate()
        InvasiveStatusElsewhere.migrate()
        PurposeEndangered.migrate()
        PurposeWeed.migrate()

    def __repr__(self):
        return '<Population %r>' % self.id

class Stage(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'stages'
    id = db.Column(db.Integer, primary_key=True, index=True)
    publication_id = db.Column(db.Integer, db.ForeignKey('publications.id',ondelete='CASCADE'))
    stage_type_id = db.Column(db.Integer, db.ForeignKey('stage_types.id',ondelete='CASCADE')) 
    name = db.Column(db.Text())

    matrix_stages = db.relationship("MatrixStage", backref="stage", passive_deletes=True)

    version = db.relationship("Version", backref="stage", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    def to_json(self, key):
        stage = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='stages', key=key,
                                      _external=False),
            'data' : {
                'species' : self.species.to_json_simple(key),
                'publication' : self.to_json_simple(key),
                'stage_type': self.to_json_simple(key),
                'name' : self.name,
                'matrix_stages' : [matrix_stage.to_json_simple(key) for matrix_stage in self.matrix_stages],
                'versions' : [version.to_json_simple(key) for version in versions]
                }
        }
        return stage

    def to_json_simple(self, key):
        stage = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='stages', key=key,
                                      _external=False),
            'data' : {
                'name' : self.name,
                'matrix_stages_len' : len(self.matrix_stages),
                'versions_len' : len(self.versions)
                }
        }
        return stage

    def __repr__(self):
        return str(self.species_id)

class StageType(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'stage_types'
    id = db.Column(db.Integer, primary_key=True, index=True)
    type_name = db.Column(db.Text())
    type_class_id = db.Column(db.Integer, db.ForeignKey('stage_type_classes.id',ondelete='CASCADE'))

    stages = db.relationship("Stage", backref="stage_types", passive_deletes=True)

    version = db.relationship("Version", backref="stage_type", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    def to_json(self, key):
        stage_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='stage_types', key=key,
                                      _external=False),
            'data' : {
                'type_name' : self.type_name,
                'type_class' : self.type_class.to_json_simple(key),
                'stages' : [stage.to_json_simple(key) for stage in self.stages],
                'versions' : [version.to_json_simple(key) for version in self.versions]
                }
        }
        return stage_type

    def to_json_simple(self, key):
        stage_type = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='stage_types', key=key,
                                      _external=False),
            'data' : {
                'type_name' : self.type_name,
                'stages_len' : len(self.stages),
                'versions_len' : len(self.versions)
                }
        }
        return stage_type

    @staticmethod
    def migrate():
        StageTypeClass.migrate()

    def __repr__(self):
        return self.type_name


class Treatment(db.Model):
    __tablename__ = 'treatments'
    id = db.Column(db.Integer, primary_key=True, index=True)
    treatment_name = db.Column(db.Text())
    
    matrices = db.relationship("Matrix", backref="treatment", passive_deletes=True)
    

    @staticmethod
    def migrate():
        with open('app/data-migrate/matrices.json') as d_file:
            data = json.load(d_file)
            json_data = data["Matrix"]
            nodes = json_data["Treatment"]

            for node in nodes:
                i = Treatment.query.filter_by(treatment_name=node['type_name']).first()
                if i is None:
                    i = Treatment()

                i.type_name = node['treatment_name']

                db.session.add(i)
                db.session.commit()

    def to_json(self, key):
        treatment= {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='treatments', key=key,
                                  _external=False),
            'data' : {
                'treatment_name' : self.treatment_name,
                'matrices' : [matrix.to_json_simple(key) for matrix in self.matrices]
            }

        }
        return treatment

    def to_json_simple(self, key):
        treatment= {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='treatments', key=key,
                                  _external=False),
            'data' : {
                'treatment_name' : self.treatment_name,
                'matrices_len' : len(self.matrices)
            }

        }
        return treatment


    def __repr__(self):
        return self.treatment_name if self.treatment_name else ''

class MatrixStage(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'matrix_stages'
    id = db.Column(db.Integer, primary_key=True)
    stage_order = db.Column(db.SmallInteger())
    stage_id = db.Column(db.Integer, db.ForeignKey('stages.id',ondelete='CASCADE'))

    matrix_id = db.Column(db.Integer, db.ForeignKey('matrices.id',ondelete='CASCADE'))

    version = db.relationship("Version", backref="matrix_stage", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    def to_json(self, key):
        matrix_stage = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrix_stages', key=key,
                                      _external=False),
            'data' : {
                'stage_order' : self.stage_order,
                'stage' : self.stage.to_json_simple(key),
                'matrix' : self.matrix.to_json_simple(key),
                'versions' : [version.to_json_simple(key) for version in self.versions]
                }
        }
        return matrix_stage

    def to_json_simple(self, key):
        matrix_stage = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrix_stages', key=key,
                                      _external=False),
            'data' : {
                'stage_order' : self.stage_order,
                'versions_len' : len(self.versions)
                }
        }
        return matrix_stage

    def __repr__(self):
        return '<Matrix Stage %r>' % self.stage_order

class MatrixValue(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'matrix_values'
    id = db.Column(db.Integer, primary_key=True)
    column_number = db.Column(db.SmallInteger())
    row_number = db.Column(db.SmallInteger())
    transition_type_id = db.Column(db.Integer, db.ForeignKey('transition_types.id',ondelete='CASCADE'))
    value = db.Column(db.Float())

    matrix_id = db.Column(db.Integer, db.ForeignKey('matrices.id',ondelete='CASCADE'))

    version = db.relationship("Version", backref="matrix_value", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    @staticmethod
    def migrate():
        TransitionType.migrate()

    def to_json(self, key):
        matrix_value = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrix_values', key=key,
                                      _external=False),
            'data' : {
                'column_number' : self.column_number,
                'row_number' : self.row_number,
                'transition_type' : self.transition_type.to_json_simple(key),
                'value' : self.value,
                'matrix' : self.matrix.to_json_simple(key),
                'versions' : [version.to_json_simple(key) for version in self.versions]
            }
        }
        return matrix_value

    def to_json_simple(self, key):
        matrix_value = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrix_values', key=key,
                                      _external=False),
            'data' : {
                'column_number' : self.column_number,
                'row_number' : self.row_number,
                'transition_type' : self.transition_type.type_name,
                'value' : self.value,
                'versions_len' : len(self.versions)
            }
        }
        return matrix_value

    def __repr__(self):
        return self.column_number

class Matrix(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'matrices'
    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(200), index=True, unique=True)
    population_id = db.Column(db.Integer, db.ForeignKey('populations.id',ondelete='CASCADE'))
    treatment_id = db.Column(db.Integer, db.ForeignKey('treatments.id',ondelete='CASCADE'))
    matrix_split = db.Column(db.Boolean())
    matrix_composition_id = db.Column(db.Integer, db.ForeignKey('matrix_compositions.id',ondelete='CASCADE'))
    seasonal = db.Column(db.Boolean())
    survival_issue = db.Column(db.Float())
    matrix_irreducible = db.Column(db.Boolean())
    matrix_primitive = db.Column(db.Boolean())
    matrix_ergodic = db.Column(db.Boolean())
    periodicity = db.Column(db.Float())
    n_intervals = db.Column(db.SmallInteger())
    matrix_criteria_size = db.Column(db.String(200))
    matrix_criteria_ontogeny = db.Column(db.Boolean())
    matrix_criteria_age = db.Column(db.Boolean())
    matrix_start_year = db.Column(db.Integer)
    matrix_start_month = db.Column(db.Integer())
    matrix_end_year = db.Column(db.Integer)
    matrix_end_month = db.Column(db.Integer())
    matrix_start_season_id = db.Column(db.Integer, db.ForeignKey('start_seasons.id',ondelete='CASCADE')) # Proto says season used as described in manuscript, maybe not safe to derive this from latdeg, country, date
    matrix_end_season_id = db.Column(db.Integer, db.ForeignKey('end_seasons.id',ondelete='CASCADE')) # Proto says season used as described in manuscript, maybe not safe to derive this from latdeg, country, date
    matrix_fec = db.Column(db.Boolean())
    matrix_a_string = db.Column(db.Text())
    matrix_u_string = db.Column(db.Text())
    matrix_f_string = db.Column(db.Text())
    matrix_c_string = db.Column(db.Text())
    matrix_difficulty = db.Column(db.String(64))
    matrix_complete = db.Column(db.Boolean())
    independence_origin = db.Column(db.Text())
    #n_plots = db.Column(db.SmallInteger()) # Danny/Jenni/Dave, will need your help with plots too - not quite sure what they are.
    #plot_size = db.Column(db.Float()) # Schema states, 'R convert to m^2'
    #n_individuals = db.Column(db.Integer()) # Schema states, 'total number of individuals observed'
    studied_sex_id = db.Column(db.Integer, db.ForeignKey('studied_sex.id',ondelete='CASCADE'))
    captivity_id = db.Column(db.Integer, db.ForeignKey('captivities.id',ondelete='CASCADE'))
    matrix_dimension = db.Column(db.Integer()) # dimension of matrix population A   
    observations = db.Column(db.Text())

    class_organized = db.Column(db.Text())
    class_author = db.Column(db.Text())
    class_number = db.Column(db.Text())

    #vectors_includes_na = db.Column(db.Text())
    matrix_lambda = db.Column(db.Float())
    
    independent = db.Column(db.Boolean())
    non_independence = db.Column(db.Text())
    non_independence_author = db.Column(db.Text())

    intervals = db.relationship("Interval", backref="matrix",passive_deletes=True)
    matrix_values = db.relationship("MatrixValue", backref="matrix", passive_deletes=True)
    matrix_stages = db.relationship("MatrixStage", backref="matrix", passive_deletes=True)
    fixed = db.relationship("Fixed", backref="matrix", passive_deletes=True)
    seeds = db.relationship("Seed", backref="matrix", passive_deletes=True)

    # Versioning
    version = db.relationship("Version", backref="matrix", passive_deletes=True)
    #version_latest = db.Column(db.String(64))
    #version_original = db.Column(db.Boolean())
    #version_ok = db.Column(db.Boolean)

    @staticmethod
    def migrate():
        MatrixComposition.migrate()
        StartSeason.migrate()
        EndSeason.migrate()
        StudiedSex.migrate()
        Captivity.migrate()
        Status.migrate()

    # Generate UID for this Matrix
    def create_uid(self):
        import re
        species_accepted = self.population.species.species_accepted
        journal = self.population.publication.pub_title
        year_pub = self.population.publication.year
        authors = self.population.publication.authors[:15].encode('utf-8')
        pop_name = self.population.name.encode('utf-8')
        
        try:
            composite = self.matrix_composition.comp_name
        except AttributeError:
            composite = ''

        # treatment = self.treatment.treatment_name
        try:
            start_year = self.matrix_start[-4:]
        except TypeError:
            start_year = ''

        # observation = self.observations.encode('utf-8')
        # matrix_a_string = self.matrix_a_string

        import time
        timestamp = time.time()
        uid_concat = '{}{}{}{}{}{}{}{}'.format(species_accepted, journal, year_pub, authors, pop_name, composite, start_year, timestamp)
        uid_lower = uid_concat.lower()
        uid = re.sub('[\W_]+', '', uid_lower)

        self.uid = uid
        if Matrix.query.filter_by(uid=uid).first() == None:
            pass
            # db.session.add(self)
            # db.session.commit()
        else:
            return 

    def to_json(self, key):       
        matrix = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrices', key=key,
                                  _external=False),
            'data' : {
                'population' : self.population.to_json_simple(key),
                'treatment' : self.treatment.to_json_simple(key),
                'matrix_split' : self.matrix_split,
                'matrix_composition' : self.matrix_composition.to_json_simple(key),
                'survival_issue' : self.survival_issue,
                'n_intervals' : self.n_intervals,
                'periodicity' : self.periodicity,
                'matrix_criteria_size' : self.matrix_criteria_size,
                'matrix_criteria_ontogeny' : self.matrix_criteria_ontogeny,
                'matrix_criteria_age' : self.matrix_criteria_age,
                'matrix_start_season' : self.start_season.to_json_simple(key) if self.start_season else None,
                'matrix_end_season' : self.end_season.to_json_simple(key) if self.end_season else None,
                'matrix_fec' : self.matrix_fec,
                'matrix_a_string' : self.matrix_a_string,
                'matrix_u_string' : self.matrix_u_string,
                'matrix_f_string' : self.matrix_f_string,
                #'n_plots' : self.n_plots,
                #'plot_size' : self.plot_size,
                'studied_sex' : self.studied_sex.to_json_simple(key),
                'captivities' : self.captivities.to_json_simple(key) if self.captivities else None,
                'matrix_dimension' : self.matrix_dimension,
                'observations' : self.observations,
                'uid' : self.uid,
                'seasonal' : self.seasonal,
                'survival_issue' : self.survival_issue,
                'matrix_start_year' : self.matrix_start_year,
                'matrix_start_month' : self.matrix_start_month,
                'matrix_end_year' : self.matrix_end_year,
                'matrix_end_month' : self.matrix_end_month,
                
                #'n_individuals' : self.n_individuals,
                'class_organized' : self.class_organized,
                'class_author' : self.class_author,
                'class_number' : self.class_number,
                'intervals' : [interval.to_json_simple(key) for interval in self.intervals] if self.intervals else [],
                'matrix_values' : [matrix_value.to_json_simple(key) for matrix_value in self.matrix_values] if self.matrix_values else [],
                'matrix_stages' : [matrix_stage.to_json_simple(key) for matrix_stage in self.matrix_stages] if self.matrix_stages else [],
                'seeds' : [seeds.to_json_simple(key) for seeds in self.seeds] if self.seeds else []
                #'versions' : [versions.to_json_simple(key) for versions in self.versions] if self.versions else []
            }
        }

        user = User.query.filter_by(api_hash=key).first()
        if user is not None and user.institute.institution_name == "University of Exeter":   
            matrix['data']['matrix_difficulty'] = self.matrix_difficulty #
            matrix['data']['matrix_complete'] = self.matrix_complete #
            matrix['data']['independence_origin'] = self.independence_origin #
            #matrix['data']['vectors_includes_na'] = self.vectors_includes_na #
            matrix['data']['independent'] = self.independent #
            matrix['data']['non_independence'] = self.non_independence #
            matrix['data']['non_independence_author'] = self.non_independence_author #
            matrix['data']['fixed'] = [fixed.to_json_simple(key) for fixed in self.fixed] if self.fixed else []

        return matrix

    def to_json_simple(self, key):       
        matrix = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='matrices', key=key,
                                  _external=False),
            'data' : {
                'treatment' : self.treatment.to_json_simple(key),
                'matrix_split' : self.matrix_split,
                'matrix_composition' : self.matrix_composition.to_json_simple(key) if self.matrix_composition else None,
                'survival_issue' : self.survival_issue,
                'matrix_a_string' : self.matrix_a_string,
                'matrix_dimension' : self.matrix_dimension,
                'observations' : self.observations,
                'uid' : self.uid,
                'seasonal' : self.seasonal,
                'matrix_start_year' : self.matrix_start_year,
                'matrix_end_year' : self.matrix_end_year,
                'intervals_count' : len(self.intervals),
                'matrix_values_len' : len(self.matrix_values),
                'matrix_stages_len' : len(self.matrix_stages),
                'fixed_len' : len(self.fixed),
                'seeds_len' : len(self.seeds),
                #'versions_len' : len(self.versions)
            }
        }

        user = User.query.filter_by(api_hash=key).first()
        if user is not None and user.institute.institution_name == "University of Exeter":
            matrix['data']['matrix_difficulty'] = self.matrix_difficulty, #
            matrix['data']['matrix_complete'] = self.matrix_complete, #
            matrix['data']['independence_origin'] = self.independence_origin, #
            #matrix['data']['vectors_includes_na'] = self.vectors_includes_na, #
            matrix['data']['independent'] = self.independent, #
            matrix['data']['non_independence'] = self.non_independence, #
            matrix['data']['non_independence_author'] = self.non_independence_author, #
            matrix['data']['fixed_len'] = len(self.fixed)
        return matrix

    def __repr__(self):
        return '<Matrix %r>' % self.id
    
    def add_to_logger(self,current_user,field_name,content_before,content_after,new_edit_delete):
        changelogger = {
            'user_id' : current_user.id,
            'object_type' : "matrix",
            'field_name' : field_name,
            'object_id' : self.id,
            'content_before' : content_before,
            'content_after' : content_after,
            'new_edit_delete' : new_edit_delete
        }
        
        cl = ChangeLogger(**changelogger)
        if cl.content_before != cl.content_after:
            if cl.content_before == None:
                cl.new_edit_delete = "new"

            db.session.add(cl)
            db.session.commit()

''' This table only applies to mean matrices, to identify the intervals that the mean values are derived from '''
class Interval(db.Model):
    __tablename__ = 'intervals'
    id = db.Column(db.Integer, primary_key=True)
    matrix_id = db.Column(db.Integer, db.ForeignKey('matrices.id',ondelete='CASCADE'))
    interval_order = db.Column(db.Integer())
    interval_start = db.Column(db.Date())
    interval_end = db.Column(db.Date())

    def to_json(self, key):
        interval = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='intervals', key=key,
                                  _external=False),
            'data' : {
                'interval_order' : self.interval_order,
                'interval_start' : self.interval_start,
                'interval_end' : self.interval_end,
                'matrix' : self.matrix.to_json_simple(key)
            }
        }
        return interval

    def to_json_simple(self, key):
        interval = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='intervals', key=key,
                                  _external=False),
            'data' : {
                'interval_order' : self.interval_order,
                'interval_start' : self.interval_start,
                'interval_end' : self.interval_end,
                'matrix' : self.matrix.to_json_simple(key)
            }
        }
        return interval

    def __repr__(self):
        return '<Interval %r>' % self.id

''' Fixed Stuff - Not yet agreed but CORE committee to be released'''
class Fixed(db.Model):
    #query_class = VersionQuery
    __tablename__ = 'fixed'
    id = db.Column(db.Integer, primary_key=True)
    matrix_id = db.Column(db.Integer, db.ForeignKey('matrices.id',ondelete='CASCADE'), index=True)
    vector_str = db.Column(db.Text())
    vector_present = db.Column(db.Boolean())
    total_pop_no = db.Column(db.Integer())
    small_id = db.Column(db.Integer, db.ForeignKey('smalls.id',ondelete='CASCADE'))
    census_timing_id = db.Column(db.Integer, db.ForeignKey('census_timings.id',ondelete='CASCADE'))
    seed_stage_error = db.Column(db.Boolean(), default=False)
    private = db.Column(db.Boolean(), default=True)
    vectors_includes_na = db.Column(db.Boolean(), default=False)
    vectors_proportional = db.Column(db.Text())
    vector_class_names = db.Column(db.Text())
    


    version = db.relationship("Version", backref="fixed", passive_deletes=True)

    @staticmethod
    def migrate():
        CensusTiming.migrate()
        Small.migrate()

    def to_json(self, key):
        fixed = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='fixed', key=key,
                                  _external=False),
            'data' : {
                'matrix' : self.matrix.to_json_simple(key),
                'vector_str' : self.vector_str,
                'vector_present' : self.vector_present,
                'total_pop_no' : self.total_pop_no,
                'small' : self.smalls.to_json_simple(key) if self.smalls else None,
                'census' : self.census_timings.to_json_simple(key) if self.census_timings else None,
                'seed_stage_error' : self.seed_stage_error,
                'private' : self.private,                      
                'versions' : [version.to_json_simple(key) for version in self.versions]
            }
        }

        user = User.query.filter_by(api_hash=key).first()
        if user is not None and user.institute.institution_name == "University of Exeter":
            return fixed
        else:
            from api_1_0.errors import unauthorized
            return unauthorized("Invalid Permissions")

    def to_json_simple(self, key):
        fixed = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='fixed', key=key,
                                  _external=False),
            'data' : {
                'vector_str' : self.vector_str,
                'vector_present' : self.vector_present,
                'total_pop_no' : self.total_pop_no,
                'seed_stage_error' : self.seed_stage_error,
                'private' : self.private,                      
                'versions_len' : len(self.versions)
            }
        }
        return fixed

    def __repr__(self):
        return str(self.matrix_id)
    
    def add_to_logger(self,current_user,field_name,content_before,content_after,new_edit_delete):
        changelogger = {
            'user_id' : current_user.id,
            'object_type' : "fixed",
            'field_name' : field_name,
            'object_id' : self.id,
            'content_before' : content_before,
            'content_after' : content_after,
            'new_edit_delete' : new_edit_delete
        }
        
        cl = ChangeLogger(**changelogger)
        if cl.content_before != cl.content_after:
            if cl.content_before == None:
                cl.new_edit_delete = "new"

            db.session.add(cl)
            db.session.commit()

class Seed(db.Model):
    __tablename__ = 'seeds'
    id = db.Column(db.Integer, primary_key=True)
    matrix_id = db.Column(db.Integer, db.ForeignKey('matrices.id'), index=True)
    matrix_a = db.Column(db.Text())

    def to_json(self):
        seeds = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='seeds', key=key,
                                  _external=False),
            'data' : {
                'matrix' : self.matrix.to_json_simple(key),
                'matrix_a' : self.matrix_a,
            }
        }

        return seeds

    def to_json_simple(self):
        seeds = {
            'request_url' : url_for('api.get_one_entry', id=self.id, model='seeds', key=key,
                                  _external=False),
            'data' : {
                'matrix_a' : self.matrix_a
            }
        }

        return seeds

    def __repr__(self, key):
        return '<Seed %r>' % self.id


class Version(db.Model):
    __tablename__ = 'versions'
    id = db.Column(db.Integer, primary_key=True)
    version_number = db.Column(db.Integer(), default=0)

    #version of is the ID of the previous version, so each version can refer back to it
    #original_version_id = db.Column(db.Integer, db.ForeignKey('versions.id', ondelete='CASCADE')) 
    #version_date_added = db.Column(db.Date())
    #version_timestamp_created = db.Column(db.DateTime, default=datetime.utcnow)
    
    # If this is the original version, it will have other versions
    #original_version = db.relationship("Version", backref="child_versions", remote_side="Version.id", uselist=True)
    checked = db.Column(db.Boolean())
    
    status_id = db.Column(db.Integer, db.ForeignKey('statuses.id',ondelete='CASCADE'))
    checked_count = db.Column(db.Integer(), default=0)

    # Utility relationships
    #version_user_id = db.Column(db.Integer, db.ForeignKey('users.id',ondelete='CASCADE'))
    database_id = db.Column(db.Integer, db.ForeignKey('databases.id',ondelete='CASCADE'))
    
    entered_by_id = db.Column(db.Integer, db.ForeignKey('users.id',ondelete='CASCADE'))
    #checked_by_id = db.Column(db.Integer, db.ForeignKey('users.id',ondelete='CASCADE'))
    

    # Demography relationships
    species_id = db.Column(db.Integer, db.ForeignKey('species.id',ondelete='CASCADE'))
    taxonomy_id = db.Column(db.Integer, db.ForeignKey('taxonomies.id',ondelete='CASCADE'))
    trait_id = db.Column(db.Integer, db.ForeignKey('traits.id',ondelete='CASCADE'))
    publication_id = db.Column(db.Integer, db.ForeignKey('publications.id',ondelete='CASCADE'))
    population_id = db.Column(db.Integer, db.ForeignKey('populations.id',ondelete='CASCADE'))
    matrix_id = db.Column(db.Integer, db.ForeignKey('matrices.id',ondelete='CASCADE'))
    fixed_id = db.Column(db.Integer, db.ForeignKey('fixed.id',ondelete='CASCADE'))

    #these are defunct
    stage_id = db.Column(db.Integer, db.ForeignKey('stages.id',ondelete='CASCADE'))
    stage_type_id = db.Column(db.Integer, db.ForeignKey('stage_types.id',ondelete='CASCADE'))

    matrix_stage_id = db.Column(db.Integer, db.ForeignKey('matrix_stages.id',ondelete='CASCADE'))
    matrix_value_id = db.Column(db.Integer, db.ForeignKey('matrix_values.id',ondelete='CASCADE'))

    author_contact_id = db.Column(db.Integer, db.ForeignKey('author_contacts.id',ondelete='CASCADE'))
    additional_source_id = db.Column(db.Integer, db.ForeignKey('additional_sources.id',ondelete='CASCADE')) 
    


    def to_json(self, key):
        version = {
            'created_by' : self.user.to_json_simple(key),
            'version_number' : self.version_number,
            'original_version' : url_for('api.get_one_entry', id=self.original_version[0].id, model="versions", key=key,
                                  _external=False),
            'version_date_added' : self.version_date_added,
            'version_timestamp_created' : self.version_timestamp_created,
            'checked_count' : self.checked_count,
            'checked' : self.checked,
            'status' : self.statuses.to_json_simple(key),
            'versions' : [version.to_json_simple(key) for version in self.versions]
        }

        return version

    def to_json_simple(self, key):
        version = {
            'created_by_email' : self.user.email,
            'version_number' : self.version_number,
            'version_timestamp_created' : self.version_timestamp_created,
            'checked' : self.checked,
            'status' : self.statuses.status_name,
            'versions_len' : len(self.versions)
        }

        return version

    def __getitem__(self, key):
        return getattr(self, key)

    def parent_table(self):
        fk = {
            'species_id' : self.species_id,
            'taxonomy_id' : self.taxonomy_id,
            'trait_id' : self.trait_id,
            'publication_id' : self.publication_id,
            'population_id' : self.population_id,
            'matrix_id' : self.matrix_id, 
            'fixed_id' : self.fixed_id, 
            'stage_id' : self.stage_id,
            'stage_type_id' : self.stage_type_id,
            'matrix_stage_id' : self.matrix_stage_id, 
            'matrix_value_id' : self.matrix_value_id,
            'author_contact_id': self.author_contact_id,
            'additional_source_id' : self.additional_source_id,
            'protocol_id' : self.protocol_id
        }

        for f, k in fk.items():
            if k == 1:
                kwargs = {f: k}

        return kwargs

    def all(self):
        kwargs = self.parent_table()
        kwargs['statuses'] = Status.query.filter_by(status_name="Green").first()
        kwargs['checked'] = True
        return Version.query.filter_by(**kwargs).all()

    def original(self):
        kwargs = self.parent_table()
        kwargs['version_number'] = 0
        kwargs['statuses'] = Status.query.filter_by(status_name="Green").first()
        kwargs['checked'] = True
        return Version.query.filter_by(**kwargs).first()

    def latest(self):
        kwargs = self.parent_table()
        kwargs['statuses'] = Status.query.filter_by(status_name="Green").first()
        kwargs['checked'] = True
        latest = Version.query.filter_by(**kwargs).order_by(Version.version_number.desc()).first()
        return latest


    @staticmethod
    def migrate():
        ###previously Database.migrate()
        Status.migrate()

    def __repr__(self):
        return '<Version {} {} {}>'.format(str(self.id), self.statuses.status_name, self.checked)

'''Userlists'''
class UserList(db.Model):
    __tablename__ = 'user_lists'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id',ondelete='CASCADE'))
    name = db.Column(db.Text())
    description = db.Column(db.Text())
    public = db.Column(db.Boolean())
    DOI_ISBN = db.Column(db.Text()) 
    
'''entries in userlists'''    
class UserListEntry(db.Model):
    __tablename__ = 'user_list_entries'
    id = db.Column(db.Integer, primary_key=True)
    matrix_id = db.Column(db.Integer, db.ForeignKey('matrices.id',ondelete='CASCADE'))
    exclusion = db.Column(db.Boolean())
    notes = db.Column(db.Text())
    

