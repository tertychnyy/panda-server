'''
find candidate site to distribute input datasets

'''

import re
import sys
import time
import math
import types
import random
import datetime

from dataservice.DDM import ddm
from taskbuffer.JobSpec import JobSpec
import brokerage.broker

from config import panda_config
from pandalogger.PandaLogger import PandaLogger

# logger
_logger = PandaLogger().getLogger('DynDataDistributer')

# NG datasets
ngDataTypes = ['RAW','HITS','RDO','ESD']

# excluded provenance
ngProvenance = []

# protection for max number of replicas
protectionMaxNumReplicas  = 4

# max number of waiting jobs
maxWaitingJobs = 200

# max number of waiting jobsets
maxWaitingJobsets = 2

# clouds with small T1 to make replica at T2
cloudsWithSmallT1 = ['IT']

# files in datasets
g_filesInDsMap = {}


class DynDataDistributer:

    # constructor
    def __init__(self,jobs,taskBuffer,siteMapper):
        self.jobs = jobs
        self.taskBuffer = taskBuffer
        self.siteMapper = siteMapper
        self.token = datetime.datetime.utcnow().isoformat(' ')
        self.pd2pClouds = []


    # main
    def run(self):
        try:
            self.putLog("start for %s" % self.jobs[0].PandaID)
            # use a fixed list since some clouds don't have active T2s
            self.pd2pClouds = ['CA','DE','ES','FR','IT','ND','NL','TW','UK','US']
            # check cloud
            if not self.jobs[0].cloud in self.pd2pClouds+['CERN',]:
                self.putLog("skip cloud=%s not one of PD2P clouds %s" % (self.jobs[0].cloud,str(self.pd2pClouds)))
                self.putLog("end for %s" % self.jobs[0].PandaID)
                return
            # ignore HC and group production
            if self.jobs[0].processingType in ['hammercloud','gangarobot'] or self.jobs[0].processingType.startswith('gangarobot'):
                self.putLog("skip due to processingType=%s" % self.jobs[0].processingType)
                self.putLog("end for %s" % self.jobs[0].PandaID)
                return
            # ignore HC and group production
            if not self.jobs[0].workingGroup in ['NULL',None,'']:
                self.putLog("skip due to workingGroup=%s" % self.jobs[0].workingGroup)
                self.putLog("end for %s" % self.jobs[0].PandaID)
                return
            # get input datasets
            inputDatasets = []
            for tmpJob in self.jobs:
                if tmpJob.prodSourceLabel == 'user':
                    for tmpFile in tmpJob.Files:
                        if tmpFile.type == 'input' and not tmpFile.lfn.endswith('.lib.tgz'):
                            if not tmpFile.dataset in inputDatasets:
                                inputDatasets.append(tmpFile.dataset)
            # loop over all input datasets
            for inputDS in inputDatasets:
                # only mc/data/group datasets
                moveFlag = False
                for projectName in ['mc','data','gr']:
                    if inputDS.startswith(projectName):
                        moveFlag = True
                if not moveFlag:
                    self.putLog("skip non official dataset %s" % inputDS)
                    continue
                if re.search('_sub\d+$',inputDS) != None or re.search('_dis\d+$',inputDS) != None:
                    self.putLog("skip dis/sub dataset %s" % inputDS)
                    continue
                # check type
                tmpItems = inputDS.split('.')
                if len(tmpItems) < 5:
                    self.putLog("cannot get type from %s" % inputDS)
                    continue
                if tmpItems[4] in ngDataTypes:
                    self.putLog("don't move %s : %s" % (tmpItems[4],inputDS))
                    continue
                # get candidate sites
                self.putLog("get candidates for %s" % inputDS)
                status,sitesMaps = self.getCandidates(inputDS)
                if not status:
                    self.putLog("failed to get candidates")
                    continue
                # get size of input container
                totalInputSize = 0
                if inputDS.endswith('/'):
                    status,totalInputSize = self.getDatasetSize(inputDS)
                    if not status:
                        self.putLog("failed to get size of %s" % inputDS)
                        continue
                # get number of waiting jobs and jobsets
                nWaitingJobsAll = self.taskBuffer.getNumWaitingJobsForPD2P(inputDS)
                nWaitingJobsets = self.taskBuffer.getNumWaitingJobsetsForPD2P(inputDS)
                # loop over all datasets
                usedSites = []
                for tmpDS,tmpVal in sitesMaps.iteritems():
                    self.putLog("triggered for %s" % tmpDS,sendLog=True)
                    # increment used counter
                    nUsed = self.taskBuffer.incrementUsedCounterSubscription(tmpDS)
                    # insert dummy for new dataset which is used to keep track of usage even if subscription is not made
                    if nUsed == 0:
                        self.taskBuffer.addUserSubscription(tmpDS,['DUMMY'])
                    # collect candidates
                    allCandidates = []
                    totalUserSub = 0
                    allCompPd2pSites = []
                    allOKClouds = []
                    totalSecReplicas = 0
                    allT1Candidates = []
                    totalT1Sub = 0
                    cloudCandMap = {}
                    nReplicasInCloud = {}
                    for tmpCloud,(candSites,sitesComDS,sitesPd2pDS,nUserSub,t1HasReplica,t1HasPrimary,nSecReplicas,nT1Sub) in tmpVal.iteritems():
                        self.putLog("%s sites with comp DS:%s compPD2P:%s candidates:%s nSub:%s T1:%s Pri:%s nSec:%s nT1Sub:%s" % \
                                    (tmpCloud,str(sitesComDS),str(sitesPd2pDS),str(candSites),nUserSub,t1HasReplica,t1HasPrimary,
                                     nSecReplicas,nT1Sub))
                        # add
                        totalUserSub += nUserSub
                        totalT1Sub += nT1Sub
                        allCompPd2pSites += sitesPd2pDS
                        totalSecReplicas += nSecReplicas
                        cloudCandMap[tmpCloud] = candSites
                        nReplicasInCloud[tmpCloud] = len(sitesComDS) + len(sitesPd2pDS)
                        # cloud is candidate for T1-T1 when T1 doesn't have primary or secondary replicas or old subscriptions
                        if not t1HasPrimary and nSecReplicas == 0 and nT1Sub == 0:
                            allT1Candidates.append(tmpCloud)
                        # add candidates
                        for tmpCandSite in candSites:
                            if not tmpCandSite in usedSites:
                                allCandidates.append(tmpCandSite)
                        # add clouds
                        if not tmpCloud in allOKClouds:
                            allOKClouds.append(tmpCloud)
                    self.putLog("PD2P sites with comp replicas : %s" % str(allCompPd2pSites))
                    self.putLog("PD2P T2 candidates : %s" % str(allCandidates))
                    self.putLog("PD2P # of T2 subscriptions : %s" % totalUserSub)
                    self.putLog("PD2P # of T1 secondaries   : %s" % totalSecReplicas)
                    self.putLog("PD2P # of T1 subscriptions : %s" % nT1Sub)
                    self.putLog("PD2P T1 candidates : %s" % str(allT1Candidates))
                    self.putLog("PD2P nUsed : %s" % nUsed)
                    # make any data subscriptions to EOS
                    tmpItems = inputDS.split('.')
                    if allOKClouds != [] and inputDS.startswith('data') and nUsed >= 5 and \
                       not (len(tmpItems) >= 5 and tmpItems[4] in ['ESD']):
                        self.makeSubscriptionToEOS(inputDS)
                    # get dataset size
                    retDsSize,dsSize = self.getDatasetSize(tmpDS)
                    if not retDsSize:
                        self.putLog("failed to get dataset size of %s" % tmpDS,type='error',sendLog=True)
                        continue
                    self.putLog("PD2P nWaitingJobsets : %s" % nWaitingJobsets)
                    if totalInputSize != 0:
                        self.putLog("PD2P nWaitingJobs    : %s = %s(all)*%s(dsSize)/%s(contSize)" % \
                                    (int((float(nWaitingJobsAll * dsSize) / float(totalInputSize))),
                                     nWaitingJobsAll,dsSize,totalInputSize))
                    else:
                        self.putLog("PD2P nWaitingJobs    : %s = %s(all)" % \
                                    (nWaitingJobsAll,nWaitingJobsAll))
                    # extract integer part. log10(nUsed) and log10(nUsed)+1 are used to avoid round-off error
                    intLog10nUsed = int(math.log10(nUsed))
                    useSmallT1 = None
                    # make T1-T1
                    if nUsed > 0 and int(math.log10(nUsed)) > totalSecReplicas and \
                           (nUsed == 10**intLog10nUsed or nUsed == 10**(intLog10nUsed+1)) and \
                           nT1Sub == 0 and allT1Candidates != []:
                        self.putLog("making T1-T1",sendLog=True)
                        # make subscription
                        retT1Sub,useSmallT1 = self.makeT1Subscription(allT1Candidates,tmpDS,dsSize)
                        self.putLog("done for T1-T1")                        
                    # make a copy if small cloud is used
                    if useSmallT1 != None:
                        # change candidates
                        if cloudCandMap.has_key(useSmallT1):
                            allCandidates = cloudCandMap[useSmallT1]
                        else:
                            allCandidates = []
                        self.putLog("Changed candidates due to small cloud: %s" % str(allCandidates))
                    else:
                        # set the number of PD2P replicas
                        maxSitesHaveDS = 1
                        # additional replicas
                        if nWaitingJobsets > maxWaitingJobsets:
                            # the number of waiting jobs for this dataset
                            if totalInputSize != 0:
                                # dataset in container
                                tmpN = float(nWaitingJobsAll * dsSize) / float(totalInputSize)
                            else:
                                # dataset
                                tmpN = float(nWaitingJobsAll)
                            tmpN = int(math.log10(tmpN/float(maxWaitingJobs)))
                            maxSitesHaveDS = max(maxSitesHaveDS,tmpN)
                        # protection against too many replications
                        maxSitesHaveDS = min(maxSitesHaveDS,protectionMaxNumReplicas)
                        self.putLog("PD2P maxSitesHaveDS : %s" % maxSitesHaveDS)
                        # check number of replicas                        
                        if len(allCompPd2pSites) >= maxSitesHaveDS:
                            self.putLog("skip since many T2 PD2P sites (%s>=%s) have the replica" % (len(allCompPd2pSites),maxSitesHaveDS),
                                        sendLog=True)
                            continue
                        # check the number of subscriptions
                        maxNumSubInAllCloud = max(0,maxSitesHaveDS-len(allCompPd2pSites))
                        self.putLog("PD2P maxNumSubInAllCloud : %s" % maxNumSubInAllCloud)
                        if totalUserSub >= maxNumSubInAllCloud:
                            self.putLog("skip since enough subscriptions (%s>=%s) were already made for T2 PD2P" % \
                                        (totalUserSub,maxNumSubInAllCloud),
                                        sendLog=True)
                            continue
                    # no candidates
                    if len(allCandidates) == 0:
                        self.putLog("skip since no candidates",sendLog=True)
                        continue
                    # get inverse weight for brokerage
                    weightForBrokerage = self.getWeightForBrokerage(allCandidates,tmpDS,nReplicasInCloud)
                    self.putLog("inverse weight %s" % str(weightForBrokerage))
                    # get free disk size
                    retFreeSizeMap,freeSizeMap = self.getFreeDiskSize(tmpDS,allCandidates)
                    if not retFreeSizeMap:
                        self.putLog("failed to get free disk size",type='error',sendLog=True)
                        continue
                    # run brokerage
                    tmpJob = JobSpec()
                    tmpJob.AtlasRelease = ''
                    self.putLog("run brokerage for %s" % tmpDS)
                    usedWeight = brokerage.broker.schedule([tmpJob],self.taskBuffer,self.siteMapper,True,allCandidates,
                                                           True,specialWeight=weightForBrokerage,getWeight=True,
                                                           sizeMapForCheck=freeSizeMap,datasetSize=dsSize)
                    for tmpWeightSite,tmpWeightStr in usedWeight.iteritems():
                        self.putLog("weight %s %s" % (tmpWeightSite,tmpWeightStr),sendLog=True)
                    self.putLog("site -> %s" % tmpJob.computingSite)
                    # make subscription
                    subRet,dq2ID = self.makeSubscription(tmpDS,tmpJob.computingSite)
                    self.putLog("made subscription to %s:%s" % (tmpJob.computingSite,dq2ID),sendLog=True)
                    usedSites.append(tmpJob.computingSite)
                    # update database
                    if subRet:
                        self.taskBuffer.addUserSubscription(tmpDS,[dq2ID])
            self.putLog("end for %s" % self.jobs[0].PandaID)
        except:
            errType,errValue = sys.exc_info()[:2]
            self.putLog("%s %s" % (errType,errValue),'error')


    # get candidate sites for subscription
    def getCandidates(self,inputDS):
        # return for failure
        failedRet = False,{'':{'':([],[],[],0,False,False,0,0)}}
        # get replica locations
        if inputDS.endswith('/'):
            # container
            status,tmpRepMaps = self.getListDatasetReplicasInContainer(inputDS)
            # get used datasets
            if status:
                status,tmpUsedDsList = self.getUsedDatasets(tmpRepMaps)
                # remove unused datasets
                newRepMaps = {}
                for tmpKey,tmpVal in tmpRepMaps.iteritems():
                    if tmpKey in tmpUsedDsList:
                        newRepMaps[tmpKey] = tmpVal
                tmpRepMaps = newRepMaps        
        else:
            # normal dataset
            status,tmpRepMap = self.getListDatasetReplicas(inputDS)
            tmpRepMaps = {inputDS:tmpRepMap}
        if not status:
            # failed
            self.putLog("failed to get replica locations for %s" % inputDS,'error')
            return failedRet
        # get all sites
        allSiteMap = {}
        for tmpSiteName,tmpSiteSpec in self.siteMapper.siteSpecList.iteritems():
            # check cloud
            if not tmpSiteSpec.cloud in self.pd2pClouds:
                continue
            # ignore test sites
            if 'test' in tmpSiteName.lower():
                continue
            # analysis only
            if not tmpSiteName.startswith('ANALY'):
                continue
            # online
            if not tmpSiteSpec.status in ['online']:
                self.putLog("skip %s due to status=%s" % (tmpSiteName,tmpSiteSpec.status))
                continue
            if not allSiteMap.has_key(tmpSiteSpec.cloud):
                allSiteMap[tmpSiteSpec.cloud] = []
            allSiteMap[tmpSiteSpec.cloud].append(tmpSiteSpec)
        # NG DQ2 IDs
        ngDQ2SuffixList = ['LOCALGROUPDISK']
        # loop over all clouds
        returnMap = {}
        checkedMetaMap = {}
        userSubscriptionsMap = {}
        for cloud in self.pd2pClouds:
            self.putLog("cloud=%s" % tmpSiteSpec.cloud)
            # DQ2 prefix of T1
            tmpT1SiteID = self.siteMapper.getCloud(cloud)['source']
            tmpT1DQ2ID  = self.siteMapper.getSite(tmpT1SiteID).ddm
            prefixDQ2T1 = re.sub('[^_]+DISK$','',tmpT1DQ2ID)
            # loop over all datasets     
            for tmpDS,tmpRepMap in tmpRepMaps.iteritems():
                candSites     = []
                sitesComDS    = []
                sitesCompPD2P = []
                # check metadata
                if not checkedMetaMap.has_key(tmpDS):
                    checkedMetaMap[tmpDS] = self.getDatasetMetadata(tmpDS)
                retMeta,tmpMetadata = checkedMetaMap[tmpDS]
                if not retMeta:
                    self.putLog("failed to get metadata for %s" % tmpDS,'error')
                    return failedRet
                if tmpMetadata['provenance'] in ngProvenance:
                    self.putLog("provenance=%s of %s is excluded" % (tmpMetadata['provenance'],tmpDS))
                    continue
                if tmpMetadata['hidden'] in [True,'True']:
                    self.putLog("%s is hidden" % tmpDS)
                    continue
                if tmpDS.startswith('gr') and tmpMetadata['provenance'] != 'GP':
                    self.putLog("group dataset %s is excluded since provenance='%s' != GP" % \
                                (tmpDS,tmpMetadata['provenance']))
                    continue
                # check T1 has a replica
                t1HasReplica = False
                t1HasPrimary = False
                nSecReplicas = 0
                for tmpDQ2ID,tmpStatMap in tmpRepMap.iteritems():
                    # check NG suffix
                    ngSuffixFlag = False
                    for tmpNGSuffix in ngDQ2SuffixList:
                        if tmpDQ2ID.endswith(tmpNGSuffix):
                            ngSuffixFlag = True
                            break
                    if ngSuffixFlag:
                        continue
                    if tmpDQ2ID.startswith(prefixDQ2T1):
                        if tmpStatMap[0]['total'] == tmpStatMap[0]['found']:
                            t1HasReplica = True
                        # check replica metadata to get archived info
                        retRepMeta,tmpRepMetadata = self.getReplicaMetadata(tmpDS,tmpDQ2ID)
                        if not retRepMeta:
                            self.putLog("failed to get replica metadata for %s:%s" % \
                                        (tmpDS,tmpDQ2ID),'error')
                            return failedRet
                        # check archived field
                        if isinstance(tmpRepMetadata,types.DictType) and tmpRepMetadata.has_key('archived') and \
                            tmpRepMetadata['archived'] == 'primary':
                            # primary
                            t1HasPrimary = True
                            break
                        elif isinstance(tmpRepMetadata,types.DictType) and tmpRepMetadata.has_key('archived') and \
                            tmpRepMetadata['archived'] == 'secondary':
                            # secondary
                            nSecReplicas += 1
                            break
                # get on-going subscriptions
                timeRangeSub = 7
                if not userSubscriptionsMap.has_key(tmpDS):
                    userSubscriptionsMap[tmpDS] = self.taskBuffer.getUserSubscriptions(tmpDS,timeRangeSub)
                userSubscriptions = userSubscriptionsMap[tmpDS]
                # unused cloud
                if not allSiteMap.has_key(cloud):
                    continue
                # count the number of T1 subscriptions
                nT1Sub = 0
                for tmpUserSub in userSubscriptions:
                    if tmpUserSub.startswith(prefixDQ2T1):
                        nT1Sub += 1
                # check sites
                nUserSub = 0
                for tmpSiteSpec in allSiteMap[cloud]:
                    # check cloud
                    if tmpSiteSpec.cloud != cloud:
                        continue
                    self.putLog("%s" % tmpSiteSpec.sitename)
                    # prefix of DQ2 ID
                    prefixDQ2 = re.sub('[^_]+DISK$','',tmpSiteSpec.ddm)
                    # skip T1
                    if prefixDQ2 == prefixDQ2T1:
                        continue
                    # check if corresponding DQ2 ID is a replica location
                    hasReplica = False
                    for tmpDQ2ID,tmpStatMap in tmpRepMap.iteritems():
                        # check NG suffix
                        ngSuffixFlag = False
                        for tmpNGSuffix in ngDQ2SuffixList:
                            if tmpDQ2ID.endswith(tmpNGSuffix):
                                ngSuffixFlag = True
                                break
                        if ngSuffixFlag:
                            continue
                        if tmpDQ2ID.startswith(prefixDQ2):
                            if tmpStatMap[0]['total'] == tmpStatMap[0]['found']:
                                # complete
                                sitesComDS.append(tmpSiteSpec.sitename)
                                if tmpSiteSpec.cachedse == 1:
                                    sitesCompPD2P.append(tmpSiteSpec.sitename)                                    
                            hasReplica = True
                            break
                    # site doesn't have a replica
                    if (not hasReplica) and tmpSiteSpec.cachedse == 1:
                        candSites.append(tmpSiteSpec.sitename)
                    # the number of subscriptions
                    for tmpUserSub in userSubscriptions:
                        if tmpUserSub.startswith(prefixDQ2):
                            nUserSub += 1
                            break
                # append
                if not returnMap.has_key(tmpDS):
                    returnMap[tmpDS] = {}
                returnMap[tmpDS][cloud] = (candSites,sitesComDS,sitesCompPD2P,nUserSub,t1HasReplica,t1HasPrimary,nSecReplicas,nT1Sub)
        # return
        return True,returnMap

    
    # check DDM response
    def isDQ2ok(self,out):
        if out.find("DQ2 internal server exception") != -1 \
               or out.find("An error occurred on the central catalogs") != -1 \
               or out.find("MySQL server has gone away") != -1 \
               or out == '()':
            return False
        return True
    

    # get map of DQ2 IDs
    def getDQ2ID(self,sitename,dataset):
        # get DQ2 ID
        if not self.siteMapper.checkSite(sitename):
            self.putLog("cannot find SiteSpec for %s" % sitename)
            return ''
        dq2ID = self.siteMapper.getSite(sitename).ddm
        if True:
            # data
            matchEOS = re.search('_EOS[^_]+DISK$',dq2ID)
            if matchEOS != None:
                dq2ID = re.sub('_EOS[^_]+DISK','_EOSDATADISK',dq2ID)
            else:
                dq2ID = re.sub('_[^_]+DISK','_DATADISK',dq2ID)
        else:
            # unsupported prefix for subscription
            self.putLog('%s has unsupported prefix for subscription' % dataset,'error')
            return ''
        # patch for MWT2_UC
        if dq2ID == 'MWT2_UC_DATADISK':
            dq2ID = 'MWT2_DATADISK'
        # return
        return dq2ID
        

    # get list of datasets
    def makeSubscription(self,dataset,sitename,givenDQ2ID=None):
        # return for failuer
        retFailed = False,''
        # get DQ2 IDs
        if givenDQ2ID == None:
            dq2ID = self.getDQ2ID(sitename,dataset)
        else:
            dq2ID = givenDQ2ID
        if dq2ID == '':
            self.putLog("cannot find DQ2 ID for %s:%s" % (sitename,dataset))
            return retFailed
        # make subscription    
        optSrcPolicy = 001000 | 010000
        nTry = 3
        for iDDMTry in range(nTry):
            # register subscription
            self.putLog('%s/%s registerDatasetSubscription %s %s' % (iDDMTry,nTry,dataset,dq2ID))
            status,out = ddm.DQ2.main('registerDatasetSubscription',dataset,dq2ID,0,0,{},{},optSrcPolicy,
                                      0,None,0,"production",None,'Data Brokering','secondary')
            if out.find('DQSubscriptionExistsException') != -1:
                break
            elif out.find('DQLocationExistsException') != -1:
                break
            elif status != 0 or (not self.isDQ2ok(out)):
                time.sleep(60)
            else:
                break
        # result
        if out.find('DQSubscriptionExistsException') != -1:
            pass
        elif status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response for %s' % dataset,'error')
            return retFailed
        # update 
        self.putLog('%s %s' % (status,out))
        return True,dq2ID

            
    # get weight for brokerage
    def getWeightForBrokerage(self,sitenames,dataset,nReplicasInCloud):
        # return for failuer
        retFailed = False,{}
        retMap = {}
        # get the number of subscriptions for last 24 hours
        numUserSubs = self.taskBuffer.getNumUserSubscriptions()
        # loop over all sites
        for sitename in sitenames:
            # get DQ2 ID
            dq2ID = self.getDQ2ID(sitename,dataset)
            if dq2ID == '':
                self.putLog("cannot find DQ2 ID for %s:%s" % (sitename,dataset))
                return retFailed
            # append
            if numUserSubs.has_key(dq2ID):
                retMap[sitename] = 1 + numUserSubs[dq2ID]
            else:
                retMap[sitename] = 1
            # negative weight if a cloud already has replicas
            tmpCloud = self.siteMapper.getSite(sitename).cloud
            retMap[sitename] *= (1 + nReplicasInCloud[tmpCloud])
        # return
        return retMap


    # get free disk size
    def getFreeDiskSize(self,dataset,siteList):
        # return for failuer
        retFailed = False,{}
        # loop over all sites
        sizeMap = {}
        for sitename in siteList:
            # get DQ2 IDs
            dq2ID = self.getDQ2ID(sitename,dataset)
            if dq2ID == '':
                self.putLog("cannot find DQ2 ID for %s:%s" % (sitename,dataset))
                return retFailed
            for valueItem in ['used','total']:
                nTry = 3
                for iDDMTry in range(nTry):
                    self.putLog("%s/%s queryStorageUsage key=%s value=%s site=%s" % (iDDMTry,nTry,'srm',valueItem,dq2ID))
                    status,out = ddm.DQ2.main('queryStorageUsage','srm',valueItem,dq2ID)
                    if status != 0 or (not self.isDQ2ok(out)):
                        time.sleep(60)
                    else:
                        break
                # result    
                if status != 0 or out.startswith('Error'):
                    self.putLog(out,'error')
                    self.putLog('bad DQ2 response for %s:%s' % (dq2ID,valueItem), 'error')            
                    return retFailed
                try:
                    # convert res to map
                    exec "tmpGigaVal = %s[0]['giga']" % out
                    if not sizeMap.has_key(sitename):
                        sizeMap[sitename] = {}
                    # append
                    sizeMap[sitename][valueItem] = tmpGigaVal
                    self.putLog(out)
                except:
                    self.putLog(out,'error')            
                    self.putLog('could not convert HTTP-res to free size map for %s%s' % (dq2ID,valueItem), 'error')
                    return retFailed
        # return
        self.putLog('getFreeDiskSize done->%s' % str(sizeMap))
        return True,sizeMap
            

        
    # get list of replicas for a dataset
    def getListDatasetReplicas(self,dataset):
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog("%s/%s listDatasetReplicas %s" % (iDDMTry,nTry,dataset))
            status,out = ddm.DQ2.main('listDatasetReplicas',dataset,0,None,False)
            if status != 0 or (not self.isDQ2ok(out)):
                time.sleep(60)
            else:
                break
        # result    
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response for %s' % dataset, 'error')            
            return False,{}
        try:
            # convert res to map
            exec "tmpRepSites = %s" % out
            self.putLog('getListDatasetReplicas->%s' % str(tmpRepSites))
            return True,tmpRepSites
        except:
            self.putLog(out,'error')            
            self.putLog('could not convert HTTP-res to replica map for %s' % dataset, 'error')
            return False,{}
        
    
    # get replicas for a container 
    def getListDatasetReplicasInContainer(self,container):
        # response for failure
        resForFailure = False,{}
        # get datasets in container
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s listDatasetsInContainer %s' % (iDDMTry,nTry,container))
            status,out = ddm.DQ2.main('listDatasetsInContainer',container)
            if status != 0 or (not self.isDQ2ok(out)):
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response for %s' % container, 'error')
            return resForFailure
        datasets = []
        try:
            # convert to list
            exec "datasets = %s" % out
        except:
            self.putLog('could not convert HTTP-res to dataset list for %s' % container, 'error')
            return resForFailure
        # loop over all datasets
        allRepMap = {}
        for dataset in datasets:
            # get replicas
            status,tmpRepSites = self.getListDatasetReplicas(dataset)
            if not status:
                return resForFailure
            # append
            allRepMap[dataset] = tmpRepSites
        # return
        self.putLog('getListDatasetReplicasInContainer done')
        return True,allRepMap            


    # get dataset metadata
    def getDatasetMetadata(self,datasetName):
        # response for failure
        resForFailure = False,{}
        metaDataAttrs = ['provenance','hidden']
        # get datasets in container
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s getMetaDataAttribute %s' % (iDDMTry,nTry,datasetName))
            status,out = ddm.DQ2.main('getMetaDataAttribute',datasetName,metaDataAttrs)
            if status != 0 or (not self.isDQ2ok(out)):
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response for %s' % datasetName, 'error')
            return resForFailure
        metadata = {}
        try:
            # convert to map
            exec "metadata = %s" % out
        except:
            self.putLog('could not convert HTTP-res to metadata for %s' % datasetName, 'error')
            return resForFailure
        # check whether all attributes are available
        for tmpAttr in metaDataAttrs:
            if not metadata.has_key(tmpAttr):
                self.putLog('%s is missing in %s' % (tmpAttr,str(metadata)), 'error')
                return resForFailure
        # return
        self.putLog('getDatasetMetadata -> %s' % str(metadata))
        return True,metadata


    # get replica metadata
    def getReplicaMetadata(self,datasetName,locationName):
        # response for failure
        resForFailure = False,{}
        # get metadata
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s listMetaDataReplica %s %s' % (iDDMTry,nTry,datasetName,locationName))
            status,out = ddm.DQ2.main('listMetaDataReplica',locationName,datasetName)
            if status != 0 or (not self.isDQ2ok(out)):
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response for %s' % datasetName, 'error')
            return resForFailure
        metadata = {}
        try:
            # convert to map
            exec "metadata = %s" % out
        except:
            self.putLog('could not convert HTTP-res to replica metadata for %s:%s' % \
                        (datasetName,locationName), 'error')
            return resForFailure
        # return
        self.putLog('getReplicaMetadata -> %s' % str(metadata))
        return True,metadata


    # check subscription info
    def checkSubscriptionInfo(self,destDQ2ID,datasetName):
        resForFailure = (False,False)
        # get datasets in container
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s listSubscriptionInfo %s %s' % (iDDMTry,nTry,destDQ2ID,datasetName))
            status,out = ddm.DQ2.main('listSubscriptionInfo',datasetName,destDQ2ID,0)
            if status != 0:
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response for %s' % datasetName, 'error')
            return resForFailure
        self.putLog(out)
        if out == '()':
            # no subscription
            retVal = False
        else:
            # already exists
            retVal = True
        self.putLog('checkSubscriptionInfo -> %s' % retVal)
        return True,retVal


    # get size of dataset
    def getDatasetSize(self,datasetName):
        self.putLog("get size of %s" % datasetName)
        resForFailure = (False,0)
        # get size of datasets
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s listFilesInDataset %s' % (iDDMTry,nTry,datasetName))
            status,out = ddm.DQ2.listFilesInDataset(datasetName)
            if status != 0:
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response to get size of %s' % datasetName, 'error')
            return resForFailure
        self.putLog("OK")
        # get total size
        dsSize = 0
        try:
            exec "outList = %s" % out
            for guid,vals in outList[0].iteritems():
                dsSize += long(vals['filesize'])
        except:
            self.putLog('failed to get size from DQ2 response for %s' % datasetName, 'error')
            return resForFailure
        # GB
        dsSize /= (1024*1024*1024)
        self.putLog("dataset size = %s" % dsSize)
        return True,dsSize


    # get datasets used by jobs
    def getUsedDatasets(self,datasetMap):
        resForFailure = (False,[])
        # loop over all datasets
        usedDsList = []
        for datasetName in datasetMap.keys():
            # get file list
            nTry = 3
            for iDDMTry in range(nTry):
                self.putLog('%s/%s listFilesInDataset %s' % (iDDMTry,nTry,datasetName))
                status,out = ddm.DQ2.listFilesInDataset(datasetName)
                if status != 0:
                    time.sleep(60)
                else:
                    break
            if status != 0 or out.startswith('Error'):
                self.putLog(out,'error')
                self.putLog('bad DQ2 response to get size of %s' % datasetName, 'error')
                return resForFailure
            # convert to map
            try:
                tmpLfnList = []
                exec "outList = %s" % out
                for guid,vals in outList[0].iteritems():
                    tmpLfnList.append(vals['lfn'])
            except:
                self.putLog('failed to get file list from DQ2 response for %s' % datasetName, 'error')
                return resForFailure
            # check if jobs use the dataset
            usedFlag = False
            for tmpJob in self.jobs:
                for tmpFile in tmpJob.Files:
                    if tmpFile.type == 'input' and tmpFile.lfn in tmpLfnList:
                        usedFlag = True
                        break
                # escape    
                if usedFlag:
                    break
            # used
            if usedFlag:
                usedDsList.append(datasetName)
        # return
        self.putLog("used datasets = %s" % str(usedDsList))
        return True,usedDsList


    # get file from dataset
    def getFileFromDataset(self,datasetName,guid,randomMode=False,nSamples=1):
        resForFailure = (False,None)
        # get files in datasets
        global g_filesInDsMap
        if not g_filesInDsMap.has_key(datasetName):
            nTry = 3
            for iDDMTry in range(nTry):
                self.putLog('%s/%s listFilesInDataset %s' % (iDDMTry,nTry,datasetName))
                status,out = ddm.DQ2.listFilesInDataset(datasetName)
                if status != 0:
                    time.sleep(60)
                else:
                    break
            if status != 0 or out.startswith('Error'):
                self.putLog(out,'error')
                self.putLog('bad DQ2 response to get size of %s' % datasetName, 'error')
                return resForFailure
            # get file
            try:
                exec "outList = %s" % out
                # append
                g_filesInDsMap[datasetName] = outList[0]
            except:
                self.putLog('failed to get file list from DQ2 response for %s' % datasetName, 'error')
                return resForFailure
        # random mode
        if randomMode:
            tmpList = g_filesInDsMap[datasetName].keys()
            random.shuffle(tmpList)
            retList = []
            for iSamples in range(nSamples):
                if iSamples < len(tmpList):
                    guid = tmpList[iSamples]
                    retMap = g_filesInDsMap[datasetName][guid]
                    retMap['guid'] = guid
                    retMap['dataset'] = datasetName
                    retList.append(retMap)
            return True,retList        
        # return
        if g_filesInDsMap[datasetName].has_key(guid):
            retMap = g_filesInDsMap[datasetName][guid]
            retMap['guid'] = guid
            retMap['dataset'] = datasetName            
            return True,retMap
        return resForFailure
        
        
    # make subscriptions to EOS 
    def makeSubscriptionToEOS(self,datasetName):
        self.putLog("start making EOS subscription for %s" % datasetName)
        destDQ2IDs = ['CERN-PROD_EOSDATADISK']
        # get dataset replica locations
        if datasetName.endswith('/'):
            statRep,replicaMaps = self.getListDatasetReplicasInContainer(datasetName)
        else:
            statRep,replicaMap = self.getListDatasetReplicas(datasetName)
            replicaMaps = {datasetName:replicaMap}
        if not statRep:
            self.putLog("failed to get replica map for EOS",type='error')
            return False
        # loop over all datasets
        for tmpDsName,replicaMap in replicaMaps.iteritems():
            # check if replica is already there
            for destDQ2ID in destDQ2IDs:
                if replicaMap.has_key(destDQ2ID):
                    self.putLog("skip EOS sub for %s:%s since replica is already there" % (destDQ2ID,tmpDsName))
                else:
                    statSubEx,subExist = self.checkSubscriptionInfo(destDQ2ID,tmpDsName)
                    if not statSubEx:
                        self.putLog("failed to check subscription for %s:%s" % (destDQ2ID,tmpDsName),type='error')
                        continue
                    # make subscription
                    if subExist:
                        self.putLog("skip EOS sub for %s:%s since subscription is already there" % (destDQ2ID,tmpDsName))                    
                    else:
                        statMkSub,retMkSub = self.makeSubscription(tmpDsName,'',destDQ2ID)
                        if statMkSub:
                            self.putLog("made subscription to %s for %s" % (destDQ2ID,tmpDsName))
                        else:
                            self.putLog("failed to make subscription to %s for %s" % (destDQ2ID,tmpDsName),type='error')
        # return
        self.putLog("end making EOS subscription for %s" % datasetName)        
        return True


    # register new dataset container with datasets
    def registerDatasetContainerWithDatasets(self,containerName,files,replicaMap):
        # sort by locations
        filesMap = {}
        for tmpFile in files:
            tmpLocations = replicaMap[tmpFile['dataset']]
            tmpLocations.sort()
            tmpKey = tuple(tmpLocations)
            if not filesMap.has_key(tmpKey):
                filesMap[tmpKey] = []
            # append file
            filesMap[tmpKey].append(tmpFile)
        # register new datasets
        datasetNames = []
        tmpIndex = 1
        for tmpLocations,tmpFiles in filesMap.iteritems():
            tmpDsName = containerName[:-1] + '_%04d' % tmpIndex
            tmpRet = self.registerDatasetWithLocation(tmpDsName,tmpFiles,tmpLocations)
            # failed
            if not tmpRet:
                self.putLog('failed to register %s' % tmpDsName, 'error')
                return False
            # append dataset
            datasetNames.append(tmpDsName)
            tmpIndex += 1
        # register container
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s registerContainer %s' % (iDDMTry,nTry,containerName))
            status,out = ddm.DQ2.main('registerContainer',containerName,datasetNames)
            if status != 0 and out.find('DQDatasetExistsException') != -1:
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response to register %s' % containerName, 'error')
            return False
        # return
        return True
        
            

    # register new dataset with locations
    def registerDatasetWithLocation(self,datasetName,files,locations):
        resForFailure = False
        # get file info
        guids   = []
        lfns    = []
        fsizes  = []
        chksums = []
        for tmpFile in files:
            guids.append(tmpFile['guid'])
            lfns.append(tmpFile['lfn'])
            fsizes.append(None)
            chksums.append(None)
        # register new dataset    
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s registerNewDataset %s' % (iDDMTry,nTry,datasetName))
            status,out = ddm.DQ2.main('registerNewDataset',datasetName,lfns,guids,fsizes,chksums,
                                      None,None,None,True)
            if status != 0 and out.find('DQDatasetExistsException') != -1:
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response to register %s' % datasetName, 'error')
            return resForFailure
        # freeze dataset    
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s freezeDataset %s' % (iDDMTry,nTry,datasetName))
            status,out = ddm.DQ2.main('freezeDataset',datasetName)
            if status != 0:
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response to freeze %s' % datasetName, 'error')
            return resForFailure
        # register locations
        for tmpLocation in locations:
            nTry = 3
            for iDDMTry in range(nTry):
                self.putLog('%s/%s registerDatasetLocation %s %s' % (iDDMTry,nTry,datasetName,tmpLocation))
                status,out = ddm.DQ2.main('registerDatasetLocation',datasetName,tmpLocation,0,1,None,None,None,"7 days")
                if status != 0 and out.find('DQLocationExistsException') != -1:
                    time.sleep(60)
                else:
                    break
            if out.find('DQLocationExistsException') != -1:
                pass
            if status != 0 or out.startswith('Error'):
                self.putLog(out,'error')
                self.putLog('bad DQ2 response to freeze %s' % datasetName, 'error')
                return resForFailure
        return True


    # list datasets by file GUIDs
    def listDatasetsByGUIDs(self,guids,dsFilters):
        resForFailure = (False,{})
        # get size of datasets
        nTry = 3
        for iDDMTry in range(nTry):
            self.putLog('%s/%s listDatasetsByGUIDs' % (iDDMTry,nTry))
            status,out = ddm.DQ2.listDatasetsByGUIDs(guids)
            if status != 0:
                time.sleep(60)
            else:
                break
        if status != 0 or out.startswith('Error'):
            self.putLog(out,'error')
            self.putLog('bad DQ2 response to list datasets by GUIDs','error')
            return resForFailure
        self.putLog(out)
        # get map
        retMap = {}
        try:
            exec "outMap = %s" % out
            for guid in guids:
                tmpDsNames = []
                # GUID not found
                if not outMap.has_key(guid):
                    self.putLog('GUID=%s not found' % guid,'error')
                    return resForFailure
                # ignore junk datasets
                for tmpDsName in outMap[guid]:
                    if tmpDsName.startswith('panda') or \
                           tmpDsName.startswith('user') or \
                           tmpDsName.startswith('group') or \
                           re.search('_sub\d+$',tmpDsName) != None or \
                           re.search('_dis\d+$',tmpDsName) != None or \
                           re.search('_shadow$',tmpDsName) != None:
                        continue
                    # check with filters
                    if dsFilters != []:
                        flagMatch = False
                        for tmpFilter in dsFilters:
                            if re.search(tmpFilter,tmpDsName) != None:
                                flagMatch = True
                                break
                        # not match
                        if not flagMatch:
                            continue
                    # append
                    tmpDsNames.append(tmpDsName)
                # empty
                if tmpDsNames == []:
                    self.putLog('no datasets found for GUID=%s' % guid)
                    continue
                # duplicated
                if len(tmpDsNames) != 1:
                    self.putLog('there are multiple datasets %s for GUID:%s' % (str(tmpDsNames),guid),'error')
                    return resForFailure
                # append
                retMap[guid] = tmpDsNames[0]
        except:
            self.putLog('failed to list datasets by GUIDs','error')
            return resForFailure
        return True,retMap


    # conver event/run list to datasets
    def convertEvtRunToDatasets(self,runEvtList,dsType,streamName,dsFilters):
        self.putLog('convertEvtRunToDatasets %s %s %s' % (dsType,streamName,str(dsFilters)))
        # check data type
        failedRet = False,[]
        if dsType == 'AOD':
            streamRef = 'StreamAOD_ref'
        elif dsType == 'ESD':
            streamRef = 'StreamESD_ref'
        elif dsType == 'RAW':
            streamRef = 'StreamRAW_ref'
        else:
            self.putLog("invalid data type %s for EventRun conversion" % dsType,type='error')
            return failedRet
        # import event lookup client
        from eventLookupClient import eventLookupClient
        elssiIF = eventLookupClient()
        # loop over all events
        runEvtGuidMap = {}
        nEventsPerLoop = 500
        iEventsTotal = 0
        while iEventsTotal < len(runEvtList):
            tmpRunEvtList = runEvtList[iEventsTotal:iEventsTotal+nEventsPerLoop]
            iEventsTotal += nEventsPerLoop
            if streamName == '':
                guidListELSSI = elssiIF.doLookup(tmpRunEvtList,tokens=streamRef,extract=True)
            else:
                guidListELSSI = elssiIF.doLookup(tmpRunEvtList,stream=streamName,tokens=streamRef,extract=True)
            # failed
            if guidListELSSI == None or len(guidListELSSI) == 0:
                errStr = ''
                for tmpLine in elssiIF.output:
                    errStr += tmpLine
                self.putLog(errStr,type='error')
                self.putLog("invalid retrun from EventLookup",type='error')
                return failedRet
            # check attribute
            attrNames, attrVals = guidListELSSI
            def getAttributeIndex(attr):
                for tmpIdx,tmpAttrName in enumerate(attrNames):
                    if tmpAttrName.strip() == attr:
                        return tmpIdx
                return None
            # get index
            indexEvt = getAttributeIndex('EventNumber')
            indexRun = getAttributeIndex('RunNumber')
            indexTag = getAttributeIndex(streamRef)
            if indexEvt == None or indexRun == None or indexTag == None:
                self.putLog("failed to get attribute index from %s" % str(attrNames),type='error')
                return failedRet
            # check events
            for runNr,evtNr in tmpRunEvtList:
                paramStr = 'Run:%s Evt:%s Stream:%s' % (runNr,evtNr,streamName)
                self.putLog(paramStr)
                # collect GUIDs
                tmpguids = []
                for attrVal in attrVals:
                    if runNr == attrVal[indexRun] and evtNr == attrVal[indexEvt]:
                        tmpGuid = attrVal[indexTag]
                        # check non existing
                        if tmpGuid == 'NOATTRIB':
                            continue
                        if not tmpGuid in tmpguids:
                            tmpguids.append(tmpGuid)
                # not found
                if tmpguids == []:
                    errStr = "no GUIDs were found in Event Lookup service for %s" % paramStr
                    self.putLog(errStr,type='error')
                    return failedRet                    
                # append
                runEvtGuidMap[(runNr,evtNr)] = tmpguids
        # convert to datasets
        allDatasets  = []
        allFiles     = []
        allLocations = []
        for tmpIdx,tmpguids in runEvtGuidMap.iteritems():
            runNr,evtNr = tmpIdx
            tmpDsRet,tmpDsMap = self.listDatasetsByGUIDs(tmpguids,dsFilters)
            # failed
            if not tmpDsRet:
                self.putLog("failed to convert GUIDs to datasets",type='error')
                return failedRet
            # empty
            if tmpDsMap == {}:
                self.putLog("there is no dataset for Run:%s Evt:%s" % (runNr,evtNr),type='error')
                return failedRet
            if len(tmpDsMap) != 1:
                self.putLog("there are multiple datasets %s for Run:%s Evt:%s" % (str(tmpDsMap),runNr,evtNr),
                            type='error')
                return failedRet
            # append
            for tmpGUID,tmpDsName in tmpDsMap.iteritems():
                # collect dataset names
                if not tmpDsName in allDatasets:
                    allDatasets.append(tmpDsName)
                    # get location
                    statRep,replicaMap = self.getListDatasetReplicas(tmpDsName)
                    # failed
                    if not statRep:
                        self.putLog("failed to get locations for DS:%s" % tmpDsName,type='error')
                        return failedRet
                    # collect locations
                    for tmpLocation in replicaMap.keys():
                        if not tmpLocation in allLocations:
                            allLocations.append(tmpLocation)
                # get file info
                tmpFileRet,tmpFileInfo = self.getFileFromDataset(tmpDsName,tmpGUID)
                # failed
                if not tmpFileRet:
                    self.putLog("failed to get fileinfo for GUID:%s DS:%s" % (tmpGUID,tmpDsName),type='error')
                    return failedRet
                # collect files
                allFiles.append(tmpFileInfo)
        # return
        self.putLog('converted to %s, %s, %s' % (str(allDatasets),str(allLocations),str(allFiles)))
        return True,allDatasets,allLocations,allFiles
        

    # put log
    def putLog(self,msg,type='debug',sendLog=False):
        tmpMsg = self.token+' '+msg
        if type == 'error':
            _logger.error(tmpMsg)
        else:
            _logger.debug(tmpMsg)
        # send to logger
        if sendLog:
            tmpPandaLogger = PandaLogger()
            tmpPandaLogger.lock()
            tmpPandaLogger.setParams({'Type':'pd2p'})
            tmpLog = tmpPandaLogger.getHttpLogger(panda_config.loggername)
            # add message
            if type == 'error':
                tmpLog.error(tmpMsg)
            else:
                tmpLog.info(tmpMsg)                
            # release HTTP handler
            tmpPandaLogger.release()
            time.sleep(1)
                                                                                                                            

    # make T1 subscription
    def makeT1Subscription(self,allCloudCandidates,tmpDS,dsSize):
        useSmallT1 = None
        # no candidate
        if allCloudCandidates == []:
            return True,useSmallT1
        # convert to siteIDs
        t1Candidates = []
        t1Weights    = {}
        siteToCloud  = {}
        for tmpCloud in allCloudCandidates:
            tmpCloudSpec = self.siteMapper.getCloud(tmpCloud)
            tmpT1SiteID = tmpCloudSpec['source']
            t1Candidates.append(tmpT1SiteID)
            # use MoU share
            t1Weights[tmpT1SiteID] = tmpCloudSpec['mcshare']
            # reverse lookup
            siteToCloud[tmpT1SiteID] = tmpCloud
        # get free disk size
        retFreeSizeMap,freeSizeMap = self.getFreeDiskSize(tmpDS,t1Candidates)
        if not retFreeSizeMap:
            self.putLog("failed to get free disk size",type='error',sendLog=True)
            return False,useSmallT1
        # run brokerage
        tmpJob = JobSpec()
        tmpJob.AtlasRelease = ''
        self.putLog("run brokerage for T1-T1 for %s" % tmpDS)
        usedWeight = brokerage.broker.schedule([tmpJob],self.taskBuffer,self.siteMapper,True,t1Candidates,
                                               True,specialWeight=t1Weights,getWeight=True,
                                               sizeMapForCheck=freeSizeMap,datasetSize=dsSize,
                                               pd2pT1=True)
        self.putLog("site for T1-T1 -> %s" % tmpJob.computingSite)
        # make subscription
        subRet,dq2ID = self.makeSubscription(tmpDS,tmpJob.computingSite)
        self.putLog("made subscription for T1-T1 to %s:%s" % (tmpJob.computingSite,dq2ID),sendLog=True)
        # check if small cloud is used
        if siteToCloud[tmpJob.computingSite] in cloudsWithSmallT1:
            useSmallT1 = siteToCloud[tmpJob.computingSite]
        # update database
        if subRet:
            self.taskBuffer.addUserSubscription(tmpDS,[dq2ID])
            return True,useSmallT1
        else:
            return False,useSmallT1
            
                
            
