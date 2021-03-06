#!/usr/bin/env Rscript

library(RSVSim)  # github.com/fritzsedlazeck/SURVIVOR
library(GenomicRanges)
library(rtracklayer)
library(doParallel)
library(foreach)
library(optparse)

print(packageVersion("RSVSim"))

if(packageVersion("RSVSim") < "1.17.0") {
    stop("Need RSVSim 1.17.0")
}


##################################################
# setup 
data(weightsMechanisms, package="RSVSim")
data(weightsRepeats, package="RSVSim")


##################################################
# cmd args
option_list = list(
  make_option(c("--nHomozygousDeletions"), action="store", default="NULL", help=""),
  make_option(c("--nHeterozygousDeletions"), action="store", help="", default="NULL"),
  make_option(c("--nTandemDuplications"), action="store", help="", default="NULL"),
  make_option(c("--maxEventLength"), action="store", help="", default="NULL"),
  make_option(c("--minEventLength"), action="store", help="", default="NULL"),
  make_option(c("--outdir"), action="store", help="output dir should already exist", 
  			     default="NULL"),
  make_option(c("--fa_file"), action="store",
  			      help="original unmodified reference fasta",
			      default="NULL"),
  make_option(c("--cnv_db"), action="store", 
  			     help="regions where CNV occur (bed file format)",
			     default="NULL"),
  make_option(c("--target_chrs"),
		action="store",
		help="optional chr to narrow the simulation",
		default="NULL"),
  make_option(c("--target_bed"), action="store", help="Bed where VC will evaluate",
  				 default="NULL")
)

required_args = c("nHomozygousDeletions", "nHeterozygousDeletions",
	          "nTandemDuplications", "maxEventLength",
	          "minEventLength", "outdir", "fa_file",
                  "cnv_db", "target_bed")
  
opt = parse_args(OptionParser(option_list=option_list))

# check opts are defined
for (n in required_args){
  if ( opt[n] == "NULL" ) {
    msg = paste("parameter:", n, "must be provided. See script usage (--help)")
    stop(msg)
  }
}

print("Parse options")

# specify count for each event
nHomozygousDeletions = as.numeric(opt$nHomozygousDeletions)
nHeterozygousDeletions = as.numeric(opt$nHeterozygousDeletions)
nTandemDuplications = as.numeric(opt$nTandemDuplications)

# limit CNV event length range
minEventLength = as.numeric(opt$minEventLength)
maxEventLength = as.numeric(opt$maxEventLength)

# output directory, should already exist
outdir = opt$outdir

# reference fasta
# fa_file <- "/mnt/archive/gavinp/1000_genomes/hs37d5.mod.fa"
fa_file = opt$fa_file


# cnv database from tgv # "/home/gavinp/Downloads/GRCh37_hg19_variants_2016-05-15.allCNVs_in_20120518_targets.bed"

cnv_db = import(opt$cnv_db, format="bed") 

# if non-zero, this specifies a single chromosome to be simulated 
# filter on contig if required
if ( opt$target_chrs != "NULL"){
    cnv_db = cnv_db[seqnames(cnv_db) == opt$target_chrs]
}

# set CNV caller target bed file 
# e.g. "/mnt/archive/gavinp/1000_genomes/20120518.consensus_add50bp.chrom.bed"
target_cnv_db <- read.table(opt$target_bed)

print("RSVSim Parse options complete")

##################################################
# define function removeOverlappingRegions(regions)
# this function repeatedly removes the region that overlaps 
# the most other regions in the structure
# until there are no overlaps
removeOverlappingRegions <- function(regions){
    my_regions = regions
    # reset count; 
    maxVal = 10000
    c=countOverlaps(my_regions, my_regions)
    removed <- 0
    while(maxVal>1)
    {
        maxIndex = which.max(c)
        my_regions=my_regions[-maxIndex]
        c=countOverlaps(my_regions, my_regions)
        maxVal = max(c)
        removed <- removed+1
    }
    msg = paste("Removed", removed, "overlapping regions",
    	  	length(my_regions), "remaining regions") 
    print(msg)
    return(my_regions)
}


##################################################
# MAIN

colnames(target_cnv_db) <- c('chr', 'start', 'end')
exons <- with(target_cnv_db, GRanges(chr, IRanges(start+1, end)))

cnv_db = cnv_db[width(ranges(cnv_db))<maxEventLength]
cnv_db = cnv_db[width(ranges(cnv_db))>minEventLength]

# get cnvs that overlap at least one target region
cnv_db = cnv_db[countOverlaps(cnv_db, exons) > 1] 
initial_cnv_db = cnv_db

print("Remove overlapping regions")
# [so all the checks for overlaps further below should be redundant]
cnv_db = removeOverlappingRegions(cnv_db)
foundOverlaps = TRUE
if (length(cnv_db)<(nHomozygousDeletions+nHeterozygousDeletions+nTandemDuplications)){
    stop("Non-overlapping CNV events with length < maxEventLength are too few to support specified number of deletions/duplications ")
}

print("Ensure homozygous deletions do not overlap")
while(foundOverlaps){
    cat("Generating homozygous Deletions\n")
    homo_indices = sample(length(cnv_db), size=nHomozygousDeletions, replace=FALSE)
    homoDels = sort(cnv_db[homo_indices,])
    cnv_db = cnv_db[-homo_indices,]
    foundOverlaps=length(findOverlaps(homoDels, ignoreSelf=TRUE))>0
    cat("Found overlaps", foundOverlaps,"\n")
}
foundOverlaps = TRUE
while(foundOverlaps){
    cat("Generating heterozygous Deletions\n")
    hetero_indices = sample(length(cnv_db), size=nHeterozygousDeletions,
    		            replace=FALSE)
    heteroDels = sort(cnv_db[hetero_indices,])
    cnv_db = cnv_db[-hetero_indices,]
    foundOverlaps_tmp = length(findOverlaps(heteroDels, ignoreSelf=TRUE))>0
    foundOverlaps=(sum(countOverlaps(homoDels, heteroDels)>0) || foundOverlaps_tmp)
    cat("Found overlaps", foundOverlaps,"\n")
}

###
print("Assign the heterozygous deletions randomly, equally split to each reference")
heteroDels1_indices = sample(length(heteroDels), size=floor(nHeterozygousDeletions/2), replace=FALSE)
heteroDels1 = heteroDels[heteroDels1_indices]
heteroDels2 = heteroDels[-heteroDels1_indices]
foundOverlaps = TRUE
while(foundOverlaps){
    cat("Generating tandem duplications\n")
    dup_indices = sample(length(cnv_db), size=nTandemDuplications, replace=FALSE)
    dups = sort(cnv_db[dup_indices,])
    cnv_db = cnv_db[-dup_indices,]
    # do we need dups1 and dups2?
    foundOverlaps=(sum(countOverlaps(homoDels, dups)>0) || sum(countOverlaps(heteroDels, dups)>0) || length(findOverlaps(dups, ignoreSelf=TRUE))>0 ) 
    cat("Found overlaps", foundOverlaps,"\n")
}

dups_sizes = width(ranges(dups))

###
print("Generate 2 modified fastas for heterozygous deletions")
for (p in 1:2){
    if (p==1)
    {
        gRangeDels <- c(homoDels, heteroDels1)
        gRangeDels <- sort(gRangeDels)
    } else {
        gRangeDels <- c(homoDels, heteroDels2)
        gRangeDels <- sort(gRangeDels)
    }
    names(gRangeDels) <- paste('deletion', 1:length(gRangeDels), sep='')
    names(dups) <- paste('tandemDuplication', 1:length(dups), sep='')

    # note that the tandem duplications are random multiplicities for each reference
    # so it is likely the resulting copy numbers will not match between the genomes

    # if we have a target chromosome list
    if ( opt$target_chrs != "NULL" )
    { 
        sim = simulateSV(output=outdir, genome=fa_file, chrs=opt$target_chrs,
	      		 random=FALSE, dups=length(dups), regionsDels=gRangeDels,
			 maxDups=4, sizeDups=dups_sizes, regionsDups=dups,
			 verbose=TRUE, repeatBias=FALSE, bpSeqSize=50)
    } else {
        sim = simulateSV(output=outdir, genome=fa_file, random=FALSE,
	      		dups=length(dups), regionsDels=gRangeDels, maxDups=4,
			sizeDups=dups_sizes, regionsDups=dups, verbose=TRUE,
			repeatBias=FALSE, bpSeqSize=50)
    }    
    foo <- metadata(sim)

    foo$deletions$Name <- paste('deletion', 1:length(gRangeDels), sep='')
    foo$tandemDuplications$Name <- paste('tandemDuplication', 1:length(dups), sep='')
    rownames(foo$deletions) <- c()
    rownames(foo$tandemDuplications) <- c()
    insert.csv <- paste(outdir,"/insertions", p, ".csv", sep="")
    write.table(foo$insertions, file=insert.csv, quote=FALSE, sep="\t")
    del.csv <- paste(outdir,"/deletions", p, ".csv", sep="")
    write.table(foo$deletions, file=del.csv, quote=FALSE, sep="\t")
    dups.csv <- paste(outdir,"/tandemDuplications", p, ".csv", sep="")
    write.table(foo$tandemDuplications, file=dups.csv, quote=FALSE, sep="\t")
    orig_name <- paste(c(outdir,"/genome_rearranged.fasta"), collapse="")
    ref_name <- paste(c(outdir,"/genome_rearranged",p,".fasta"), collapse="")
    sprintf("Renaming genome_rearranged.fasta to ", ref_name)
    file.rename(orig_name, ref_name)
}
