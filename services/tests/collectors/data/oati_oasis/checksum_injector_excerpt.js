// excerpt of https://www.oasis.oati.com/websmartoasis/bundles/ChecksumInjector (2026-10-21)
function DeriveTextO(n,t){var i,u,r;if(n=n+t,i=0,n.length===0)return i;for(r=0;r<n.length;r++)u=n.charCodeAt(r),i=(i<<5)-i+u|0;return i}function tryDecodeURI(n){try{return decodeURI(n)}catch(t){return n}}function tryDecodeURIComponent(n){try{return decodeURIComponent(n)}catch(t){return n}}function t
// ...
var o=getCookie("woaUserID"),s=typeof o=="string"?o:"99Chwn9V0xUS3I",h=DeriveTextO(n,s);this.setRequestHeader("rcpt",s);this.setRequestHeader("rcpi",h);t.apply(this,arguments)}catch(c){console.log(c)}
