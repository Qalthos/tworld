"""
Manage the connection to the MongoDB server.
"""

import tornado.gen
import tornado.ioloop

import motor

class MongoMgr(object):
    def __init__(self, app):
        # Keep a link to the owning application.
        self.app = app
        self.log = self.app.log
        
        # This will be the Motor (MongoDB) connection. We'll open it in the
        # first monitor_mongo_status call.
        self.mongo = None
        self.mongoavailable = False  # true if self.mongo exists and is open
        self.mongotimerbusy = False  # true while monitor_mongo_status runs
        
        # We also manage self.app.mongodb, a MotorDatabase. This must be
        # non-None exactly when mongoavailable is true.

    def init_timers(self):
        ioloop = tornado.ioloop.IOLoop.instance()
        
        # The mongo status monitor. We set up one call immediately, and then
        # try again every three seconds.
        ioloop.add_callback(self.monitor_mongo_status)
        res = tornado.ioloop.PeriodicCallback(self.monitor_mongo_status, 3000)
        res.start()

    @tornado.gen.coroutine
    def monitor_mongo_status(self):
        if (self.mongotimerbusy):
            self.log.warning('monitor_mongo_status: already in flight; did a previous call jam?')
            return
        self.mongotimerbusy = True
        
        if (self.mongoavailable):
            try:
                res = yield motor.Op(self.mongo.admin.command, 'ping')
                if (not res):
                    self.log.error('Mongo client not alive')
                    self.mongoavailable = False
            except Exception as ex:
                self.log.error('Mongo client not alive: %s', ex)
                self.mongoavailable = False
            if (not self.mongoavailable):
                self.mongo.disconnect()
                self.mongo = None
                self.app.mongodb = None
            
        if (not self.mongoavailable):
            try:
                self.mongo = motor.MotorClient()
                res = yield motor.Op(self.mongo.open)
                ### maybe authenticate to a database?
                self.mongoavailable = True
                self.app.mongodb = self.mongo[self.app.opts.mongo_database]
                self.log.info('Mongo client open')
            except Exception as ex:
                self.mongoavailable = False
                self.app.mongodb = None
                self.log.error('Mongo client not open: %s', ex)
        
        self.mongotimerbusy = False

