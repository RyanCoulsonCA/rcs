HEADER="
<!DOCTYPE html>
<html>
<head>
  <title>RCS Docs</title>
  <meta name=\"viewport\" content=\"initial-scale=1\" />
</head>
<body>
  <h1>RCS Versions</h1>
"
FOOTER="
</body>
</html>
"

makeLi () {
  echo "$2<li><a href=\"$1/\">$1</a></li>"
}

makeUl () {
  echo "  <ul>"
  for i in `ls $1`; do if [ -d "$1/$i" ]; then makeLi "$i" "    "; fi; done
  echo "  </ul>"
}

echo $HEADER
makeUl $1
echo $FOOTER
