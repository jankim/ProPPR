import sys
import os
import re
import getopt
import random
import logging
import subprocess
import collections
import util as u

MAX_FILE_LINES_TO_ECHO = 15

# parameters for iterativeStucturedGradient and structuredGradient via
# gradientToRules: maximum ratio W_prev/W for a feature that will be
# converted to a rule, where W_prev is the previous rules's weight (in
# weight-sorted order) and W is the feature's weight

MAX_WEIGHT_RATIO = 20

# parameter for iterativeStucturedGradient: number of epochs of SGD
# to perform before computing gradient

def NUM_EPOCHS_AT_ROUND_I(i): return i+1

def lift(src,dst,opts):
    """Convert arity-two facts P(X,Y) to second-order representation rel(P,X,Y)."""
    fp = open(dst,'w')
    for line in open(src):
        line = line.strip()
        if not line or line.startswith("#"):
            fp.write(line + '\n')
        else:
            if len(line.split("\t"))!=3:
                logging.warn('bad line from %s ignored: %s' % (src,line.strip()))
            else:
                fp.write('rel\t' + line + '\n')
    logging.info('second-order version of facts from '+ src + ' stored in ' + dst)
    
def lower(src,dst,opts):
    """Convert second-order representation rel(P,X,Y) back to arity-two facts P(X,Y)."""
    fp = open(dst,'w')
    for line in open(src):
        line = line.strip()
        if not line or line.startswith("#"):
            fp.write(line + '\n')
        else:
            parts = line.split("\t")
            fp.write("\t".join(parts[1:]) + "\n")
    logging.info('first-order version of facts from '+ src + ' stored in ' + dst)

def relationsToExamples(src,dst,opts):
    #TODO sampling options
    rnd = random.Random()
    trueYs = collections.defaultdict(set)
    pairedWith = collections.defaultdict(set)
    triples = set()
    entities = set()
    rels = set()
    for line in open(src):
        (relkw,r,x,y) = line.strip().split("\t")
        trueYs[(r,x)].add(y)
        rels.add(r)
        entities.add(x)
        entities.add(y)
        triples.add((r,x,y))
        pairedWith[x].add(y)
    result = []
    for r in rels:
        for x in entities:
            query = 'interp(i_%s,%s,Y)' % (r,x)
            posParts = map(lambda y: '+interp(i_%s,%s,%s)' % (r,x,y), trueYs[(r,x)])
            #TODO randomly sample negatives?
            negParts = map(lambda y: '-interp(i_%s,%s,%s)' % (r,x,y), [y for y in pairedWith[x] if y not in trueYs[(r,x)]])
            result.append((query,posParts,negParts))
    rnd.shuffle(result)
    fp = open(dst,'w')
    for (query,posParts,negParts) in result:
        fp.write(query + '\t' + '\t'.join(posParts) + '\t' + '\t'.join(negParts) + '\n')
    logging.info('example version of facts from '+ src + ' stored in ' + dst)            

def gradientToRules(src,dst,opts):
    #two usages
    # --lhs P --rhs Q: rules in body have functor Q, rules in head have functor P
    # --lhs x --rhs_i Q1 --rhs_e Q2: rules in body have functor Q1 or Q2, depending
    # whether or not the first-order predicate is intensional (ie, ends with _i).

    #parse options
    rules = []
    rhs_i = rhs_e = opts.get('--rhs','learnedPred')
    lhs = opts.get('--lhs','learnedPred')
    if '--rhs_i' in opts: rhs_i = opts['--rhs_i']
    if '--rhs_e' in opts: rhs_e = opts['--rhs_e']

    #utilities
    def intensional(pred): return pred.startswith('i_')
    def interpFeature(feat): return feat.startswith("if(") or feat.startswith("ifInv(") or feat.startswith("chain(")

    totFeatures = 0
    totRuleFeatures = 0
    #collect weights for all the features that correspond to rules
    featureWeight = collections.defaultdict(float)
    for line in open(src):
        if not line.startswith("#"):
            totFeatures += 1
            (feature,weightStr) = line.strip().split("\t")
            if interpFeature(feature):
                totRuleFeatures += 1
                weight = float(weightStr)
                if weight<0:
                    featureWeight[feature] = min(featureWeight[feature],weight)
    
    rules = []
    totAccepted = 0
    lastWeight = None
    if featureWeight:
        for (feature,weight) in sorted(featureWeight.items(), key=lambda(f,w):w):
            if weight>=0:
                break
            if (MAX_WEIGHT_RATIO!=0 and lastWeight!=None and lastWeight/weight > MAX_WEIGHT_RATIO):
                print 'stopped collected features after seeing a gap: %g to %g' % (lastWeight,weight)
                break
            #if lastWeight: print 'collecting',feature,weight,lastWeight,'ratio',lastWeight/weight
            lastWeight = weight
            totAccepted += 1
            parts = filter(lambda x:x, re.split('\W+', feature))
            if len(parts)==3:
                (iftype,p,q) = parts
                rhs = rhs_i if intensional(q) else rhs_e
                if iftype=='if' and p!=q:
                    rules.append( "%s(%s,X,Y) :- %s(%s,X,Y) {lr_%s}." % (lhs,p,rhs,q,feature))
                elif iftype=='ifInv':
                    rules.append( "%s(%s,X,Y) :- %s(%s,Y,X) {lr_%s}." % (lhs,p,rhs,q,feature))
            elif len(parts)==4:
                (chaintype,p,q,r) = parts                
                rhsq = rhs_i if intensional(q) else rhs_e
                rhsr = rhs_i if intensional(r) else rhs_e
                if chaintype=='chain':
                    rules.append( "%s(%s,X,Y) :- %s(%s,X,Z), %s(%s,Z,Y) {lr_%s}." % (lhs,p,rhsq,q,rhsr,r,feature))
    logging.info('gradientToRules examines %d gradients %d for second-order rules and accepted %d' % (totFeatures,totRuleFeatures,totAccepted))
    fp = open(dst,'w')
    fp.write("\n".join(rules) + "\n")

def stucturedGradient(src,dst,opts):
    #work out inputs/outputs
    exampleFile = src
    learnedRuleFile = dst
    backgroundFile = optdict['--src2']
    exampleStem = optdict['--stem']

    #get the interpreter and compile it, then ground the examples
    interpFile = u.getResourceFile(opts, "sg-interp-train.ppr")
    invokeProppr(opts,'compile',interpFile)
    programFileList =  interpFile[:-4]+'.wam:'+backgroundFile
    invokeProppr(opts,'ground',exampleFile,exampleFile+".grounded",'--programFiles',programFileList,'--ternaryIndex','true')

    #store gradient in a temp file
    gradientFile = u.makeOutput(opts,exampleStem+'.gradient')
    invokeProppr(opts,'gradient',exampleFile+".grounded",gradientFile,'--epochs','1')

    #convert the gradient features to rules interp(R,X,Y) :- BODY where BODY contains calls to rel(R,X,Y).l
    gradientToRules(gradientFile, learnedRuleFile, {'--lhs':'interp','--rhs':'rel'})


def iterativeStucturedGradient(src,dst,opts):
    #work out inputs/outputs
    exampleFile = src
    learnedRuleFile = dst
    backgroundFile = optdict['--src2']
    exampleStem = optdict['--stem']
    numIters = int(optdict['--numIters'])

    #copy the initial interpreter to this directory
    baseInterpFile = u.getResourceFile(opts, "sg-interp-train.ppr")
    learnedRuleFiles = []

    #iteratively learn
    for i in range(numIters):
        logging.info('training pass %i' % i)

        #create the i-th interpreter, which contains the basic interpreter rules, plus all the learned rules
        interpFile = u.makeOutput(opts,'sg-interp_n%02d.ppr' % i)
        numAddedThisRound = _appendUniqLines([baseInterpFile]+learnedRuleFiles,interpFile)
        if numAddedThisRound==0:
            logging.info('no new rules learned in previous iteration - stopping')
            break
        u.catfile(interpFile,'Interpreter used at round %d' % i)

        #compile the interpreter
        invokeProppr(opts,'compile',interpFile)

        #ground the examples using the interpreter + learned rules
        programFileList =  interpFile[:-4]+'.wam:'+backgroundFile
        groundedFile = u.makeOutput(opts,exampleFile+".grounded")
        invokeProppr(opts,'ground',exampleFile,groundedFile,'--programFiles',programFileList,'--ternaryIndex','true')

        #compute the gradient
        gradientFile = u.makeOutput(opts,exampleStem+'_n%02d.gradient' % i)
        invokeProppr(opts,'gradient',groundedFile,gradientFile,'--epochs',str(NUM_EPOCHS_AT_ROUND_I(i)))

        #convert the gradient features to rules interp(R,X,Y) :- BODY where BODY contains calls to rel(R,X,Y).
        nextLearnedRuleFile = u.makeOutput(opts,'%s-learned_n%02d.ppr' % (exampleStem,i))
        gradientToRules(gradientFile,nextLearnedRuleFile,{'--rhs_i':'learnedPred','--rhs_e':'rel'})
        logging.info('Created rule file ' + nextLearnedRuleFile)
        u.catfile(nextLearnedRuleFile,'Rules learned in round %d' % i)

        #add this to the list of learned rules
        learnedRuleFiles.append(nextLearnedRuleFile)

    #concatenate all the learned rules, replacing the interpreter with a new one
    testInterpFile = u.getResourceFile(opts, "sg-interp-test.ppr")
    _appendUniqLines([testInterpFile] + learnedRuleFiles,learnedRuleFile)


def _appendUniqLines(inputs,output):
   previousLines = set()
   fp = open(output,'w')
   for f in inputs:
      numAddedFromLastFile = 0
      for line in open(f): 
         if line not in previousLines:
            previousLines.add(line)
            numAddedFromLastFile += 1
            fp.write(line)
   fp.close()
   return numAddedFromLastFile


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)

    #usage: the following arguments, followed by a "+" and a list 
    #of any remaining arguments to pass back to calls of the 'proppr'
    #script in invokeProppr
    argspec = ["com=","src=", "dst=", 
               "C=", "n", #global proppr opts
               "lhs=", "rhs=", #for gradientToRules
               "src2=", "numIters=", "stem=", #for iterativeStucturedGradient, structuredGradient
    ]
    try:
        optlist,args = getopt.getopt(sys.argv[1:], 'x', argspec)
    except getopt.GetoptError as err:
        print 'option error: ',str(err)
        sys.exit(-1)
    optdict = dict(optlist)
    optdict['PROPPR_ARGS'] = args[1:]

    subcommand = optdict['--com']
    src = optdict['--src']
    dst = optdict['--dst']
    if subcommand=='lift':
        lift(src,dst,optdict)
    elif subcommand=='lower':
        lower(src,dst,optdict)
    elif subcommand=='rel2ex':
        relationsToExamples(src,dst,optdict)
    elif subcommand=='grad2ppr':
        gradientToRules(src,dst,optdict)
    elif subcommand=='isg-train':
        iterativeStucturedGradient(src,dst,optdict)
    elif subcommand=='sg-train':
        stucturedGradient(src,dst,optdict)
    else:
        assert False,'does not compute '+subcommand