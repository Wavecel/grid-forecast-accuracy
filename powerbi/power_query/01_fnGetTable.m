// =============================================================================
// fnGetTable  --  the single ingestion function every table query calls
// =============================================================================
// CREATE AS: Home > New Source > Blank Query > Advanced Editor > paste this
//            Rename the query to  fnGetTable
//            Right-click > UNCHECK "Enable load"
//
// WHY A FUNCTION INSTEAD OF EIGHT NEAR-IDENTICAL QUERIES
// -----------------------------------------------------
// Without it, the URL pattern, the RelativePath fix, the Parquet/CSV switch and
// the error handling are copy-pasted eight times. The first time you need to
// change any of them you have to remember all eight places, and you will miss
// one. Writing reusable M -- rather than clicking through the UI eight times --
// is one of the clearer signals that someone actually knows Power Query.
//
// PARAMETERS
//   TableName  text, e.g. "fact_load_hour" (no extension)
// =============================================================================

let
    fnGetTable = (TableName as text) as table =>
        let
            Extension  = if UseParquet then ".parquet" else ".csv",

            // RelativePath keeps the data source STATIC from the Service's point
            // of view. See 00_README_AND_PARAMETERS.m -- this single option is
            // the difference between scheduled refresh working and not.
            Source =
                Web.Contents(
                    GitHubBaseUrl,
                    [
                        RelativePath = "data/curated/" & TableName & Extension,
                        // Bypass any intermediate cache so an hourly refresh
                        // actually sees the hour's new data rather than a
                        // cached copy from 40 minutes ago.
                        Headers = [ #"Cache-Control" = "no-cache" ]
                    ]
                ),

            Parsed =
                if UseParquet then
                    Parquet.Document( Source )
                else
                    // Csv.Document options, each one deliberate:
                    //   Delimiter=","         explicit, never inferred
                    //   Columns=null          let it read the header row
                    //   Encoding=65001        UTF-8. Guessing here mangles any
                    //                         non-ASCII city name.
                    //   QuoteStyle=Csv        respect quoted fields -- the
                    //                         generated narrative text contains
                    //                         commas, and without this it would
                    //                         shatter across columns.
                    Csv.Document(
                        Source,
                        [ Delimiter = ",", Columns = null, Encoding = 65001, QuoteStyle = QuoteStyle.Csv ]
                    ),

            Promoted =
                if UseParquet then Parsed
                else Table.PromoteHeaders( Parsed, [ PromoteAllScalars = true ] ),

            // Wrap the whole thing so a failure names the table that failed.
            // The default Power Query error is a bare HTTP code with no clue
            // which of eight queries produced it -- and at 3am on a failed
            // refresh, that clue is the whole difference.
            Result =
                try Promoted
                otherwise
                    error
                        Error.Record(
                            "DataSourceError",
                            "Failed to load '" & TableName & Extension & "' from " & GitHubBaseUrl,
                            "Check that: (1) the file exists at data/curated/ in the repo, "
                                & "(2) the repository is PUBLIC (raw.githubusercontent.com returns 404 for "
                                & "private repos without a token), and (3) GitHubBaseUrl has no trailing slash."
                        )
        in
            Result
in
    fnGetTable
