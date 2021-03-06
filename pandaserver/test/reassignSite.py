import re
import sys
import time
import datetime

import optparse

usage = "%prog [options] siteName"
optP = optparse.OptionParser(usage=usage,conflict_handler="resolve")
optP.add_option('--assigned',action='store_const',const=True,dest='assigned',
                default=False,help='reassign jobs in assigned state. Jobs in activated state are reassigned by default')
options,args = optP.parse_args()

from taskbuffer.OraDBProxy import DBProxy
# password
from config import panda_config

proxyS = DBProxy()
proxyS.connect(panda_config.dbhost,panda_config.dbpasswd,panda_config.dbuser,panda_config.dbname)

site = args[0]
import userinterface.Client as Client

# erase dispatch datasets
def eraseDispDatasets(ids):
    print "eraseDispDatasets"
    datasets = []
    # get jobs
    status,jobs = Client.getJobStatus(ids)
    if status != 0:
        return
    # gather dispDBlcoks
    for job in jobs:
        # dispatchDS is not a DQ2 dataset in US
        if job.cloud == 'US':
            continue
        # erase disp datasets for production jobs only
        if job.prodSourceLabel != 'managed':
            continue
        for file in job.Files:
            if file.dispatchDBlock == 'NULL':
                continue
            if (not file.dispatchDBlock in datasets) and \
               re.search('_dis\d+$',file.dispatchDBlock) != None:
                datasets.append(file.dispatchDBlock)
    # erase
    for dataset in datasets:
        print 'erase %s' % dataset
        status,out = ddm.DQ2.main('eraseDataset',dataset)
        print out

timeLimit = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
varMap = {}
if options.assigned:
    varMap[':jobStatus']        = 'assigned'
else:
    varMap[':jobStatus']        = 'activated'
varMap[':modificationTime'] = timeLimit
varMap[':prodSourceLabel']  = 'managed'
varMap[':computingSite']    = site
if options.assigned:
    sql = "SELECT PandaID,lockedby FROM ATLAS_PANDA.jobsDefined4 "
else:
    sql = "SELECT PandaID,lockedby FROM ATLAS_PANDA.jobsActive4 "
sql += "WHERE jobStatus=:jobStatus AND computingSite=:computingSite AND modificationTime<:modificationTime AND prodSourceLabel=:prodSourceLabel ORDER BY PandaID"
status,res = proxyS.querySQLS(sql,varMap)

print "got {0} jobs".format(len(res))

jobs = []
jediJobs = []
if res != None:
    for (id,lockedby) in res:
        if lockedby == 'jedi':
            jediJobs.append(id)
        else:
            jobs.append(id)
if len(jobs):
    nJob = 100
    iJob = 0
    while iJob < len(jobs):
        print 'reassign  %s' % str(jobs[iJob:iJob+nJob])
        Client.reassignJobs(jobs[iJob:iJob+nJob])
        iJob += nJob
if len(jediJobs) != 0:
    nJob = 100
    iJob = 0
    while iJob < len(jediJobs):
        print 'kill JEDI jobs %s' % str(jediJobs[iJob:iJob+nJob])
        Client.killJobs(jediJobs[iJob:iJob+nJob],51)
        iJob += nJob


